"""Code Explorer 的 SQLite (WAL) 增量索引与 FTS5 + CodeGraph 混合检索层。

持久化位置与 JSON 索引同目录（不写入用户 Workspace）：

    ~/.mini-harness/explorer/<workspace_id>/index.db

核心能力：
1. 增量同步（Incremental Sync）：按 ``(size, mtime_ns)`` 快速跳过未变更文件，
   变更时先核验 ``sha256`` 再局部重解析脏文件，在单事务内原子更新表结构；
2. 混合检索（Hybrid Retrieval）：基于 SQLite FTS5 BM25（支持驼峰/下划线拆分与
   中文 2-gram 分词）+ 查询词覆盖率 + CodeGraph 入度/出度中心性 RRF 融合排序，
   并为命中符号附带直接 callers / callees。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mini_harness.workspace import Workspace, WorkspaceError, WorkspacePathError

from .base import LanguageAdapter
from .codegraph import (
    CodeGraph,
    GraphEdge,
    UnresolvedReference,
    build_code_graph,
)
from .models import (
    CallRecord,
    DiagnosticRecord,
    FileRecord,
    ImportRecord,
    ParsedFile,
    ProjectIndex,
    SCHEMA_VERSION,
    SourceFile,
    SymbolRecord,
)
from .scanner import (
    DEFAULT_MAX_FILE_BYTES,
    ProjectScanner,
    _decode_source,
)
from .store import DEFAULT_INDEX_ROOT

SQLITE_SCHEMA_VERSION = 4

_MAX_SNIPPET_LINES = 10
_MAX_SNIPPET_CHARS = 600
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CAMEL_SPLIT_RE = re.compile(r"([a-z0-9])([A-Z])|([A-Z]+)([A-Z][a-z])")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


class SQLiteIndexError(RuntimeError):
    """SQLite 索引读写或查询失败。"""


@dataclass(frozen=True, slots=True)
class SyncStats:
    """一次增量同步的可观测统计。"""

    total_files: int
    unchanged_files: int
    reparsed_files: int
    added_files: int
    updated_files: int
    deleted_files: int
    edges_rebuilt: bool
    duration_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "totalFiles": self.total_files,
            "unchangedFiles": self.unchanged_files,
            "reparsedFiles": self.reparsed_files,
            "addedFiles": self.added_files,
            "updatedFiles": self.updated_files,
            "deletedFiles": self.deleted_files,
            "edgesRebuilt": self.edges_rebuilt,
            "durationMs": round(self.duration_ms, 2),
        }


@dataclass(slots=True)
class IncrementalSyncResult:
    """增量同步并加载完整索引与 CodeGraph 的结果。"""

    index: ProjectIndex
    graph: CodeGraph
    stats: SyncStats


@dataclass(frozen=True, slots=True)
class CodeSearchHit:
    """混合检索返回的单条符号命中记录。"""

    symbol_id: str
    kind: str
    name: str
    qualified_name: str
    file_path: str
    start_line: int
    end_line: int
    signature: str | None
    docstring: str | None
    language: str
    score: float
    bm25_rank: int
    in_degree: int
    out_degree: int
    matched_terms: int
    total_terms: int
    callers: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbolId": self.symbol_id,
            "kind": self.kind,
            "name": self.name,
            "qualifiedName": self.qualified_name,
            "filePath": self.file_path,
            "startLine": self.start_line,
            "endLine": self.end_line,
            "signature": self.signature,
            "docstring": self.docstring,
            "language": self.language,
            "score": round(self.score, 4),
            "bm25Rank": self.bm25_rank,
            "inDegree": self.in_degree,
            "outDegree": self.out_degree,
            "matchedTerms": self.matched_terms,
            "totalTerms": self.total_terms,
            "callers": list(self.callers),
            "callees": list(self.callees),
        }


def tokenize_for_fts(text: str | None) -> str:
    """将代码标识符与中英文文本展开为 FTS5 友好的分词串。

    - 拆分 CamelCase 与 snake_case，同时保留原标识符便于精确匹配加权；
    - 对中文连续片段生成重叠 2-gram 与单字，避免 unicode61 把整句中文当作单 token。
    """
    if not text:
        return ""
    tokens: list[str] = []

    for match in _ASCII_WORD_RE.finditer(text):
        raw = match.group(0)
        lower_raw = raw.lower()
        tokens.append(lower_raw)
        # 展开下划线与驼峰子词
        for underscore_part in raw.split("_"):
            if not underscore_part:
                continue
            camel_expanded = _CAMEL_SPLIT_RE.sub(r"\1\3 \2\4", underscore_part)
            for piece in camel_expanded.split():
                piece_lower = piece.lower()
                if piece_lower and piece_lower != lower_raw:
                    tokens.append(piece_lower)

    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            tokens.append(run)
            continue
        for i in range(len(run) - 1):
            tokens.append(run[i : i + 2])
        if 2 < len(run) <= 6:
            tokens.append(run)

    return " ".join(tokens)


def _extract_query_groups(query: str) -> list[list[str]]:
    """把自然语言/代码查询拆成若干语义词组，每组包含 FTS 检索 token。"""
    groups: list[list[str]] = []
    for raw_part in re.split(r"[\s/,;:()\[\]{}|]+", query.strip()):
        if not raw_part:
            continue
        group: list[str] = []
        for match in _ASCII_WORD_RE.finditer(raw_part):
            word = match.group(0)
            lower_word = word.lower()
            if lower_word not in group:
                group.append(lower_word)
            for sub in word.split("_"):
                if not sub:
                    continue
                expanded = _CAMEL_SPLIT_RE.sub(r"\1\3 \2\4", sub)
                for piece in expanded.split():
                    p = piece.lower()
                    if len(p) >= 2 and p not in group:
                        group.append(p)
        for match in _CJK_RUN_RE.finditer(raw_part):
            run = match.group(0)
            if len(run) == 1:
                if run not in group:
                    group.append(run)
            else:
                for i in range(len(run) - 1):
                    bg = run[i : i + 2]
                    if bg not in group:
                        group.append(bg)
        if group:
            groups.append(group)
    return groups


def _quote_fts_token(token: str) -> str:
    escaped = token.replace('"', '""')
    return f'"{escaped}"'


def _extract_symbol_snippet(lines: list[str], symbol: SymbolRecord) -> str:
    """截取符号定义头部片段供 FTS5 索引，控制存储与噪声。"""
    if not lines:
        return ""
    start = max(0, symbol.start_line - 1)
    max_lines = 6 if symbol.kind in ("module", "class") else _MAX_SNIPPET_LINES
    end = min(len(lines), symbol.end_line, start + max_lines)
    snippet = "\n".join(lines[start:end])
    if len(snippet) > _MAX_SNIPPET_CHARS:
        return snippet[:_MAX_SNIPPET_CHARS]
    return snippet


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY,
            language TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            line_count INTEGER NOT NULL,
            parse_status TEXT NOT NULL,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS symbols (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            signature TEXT,
            docstring TEXT,
            decorators_json TEXT NOT NULL,
            bases_json TEXT NOT NULL,
            parent_id TEXT,
            language TEXT NOT NULL,
            snippet TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
        CREATE INDEX IF NOT EXISTS idx_symbols_parent ON symbols(parent_id);

        CREATE TABLE IF NOT EXISTS imports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            module TEXT NOT NULL,
            name TEXT,
            alias TEXT,
            level INTEGER NOT NULL,
            line INTEGER NOT NULL,
            col INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file_path);

        CREATE TABLE IF NOT EXISTS calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            caller_id TEXT NOT NULL,
            callee_expression TEXT NOT NULL,
            callee_name TEXT NOT NULL,
            line INTEGER NOT NULL,
            col INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_calls_file ON calls(file_path);

        CREATE TABLE IF NOT EXISTS diagnostics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT,
            code TEXT NOT NULL,
            message TEXT NOT NULL,
            severity TEXT NOT NULL,
            line INTEGER,
            col INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_diagnostics_file ON diagnostics(file_path);

        CREATE TABLE IF NOT EXISTS edges (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            target TEXT NOT NULL,
            kind TEXT NOT NULL,
            confidence TEXT NOT NULL,
            line INTEGER,
            detail TEXT,
            occurrences INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
        CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);

        CREATE TABLE IF NOT EXISTS unresolved (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            source_id TEXT NOT NULL,
            expression TEXT NOT NULL,
            reason TEXT NOT NULL,
            line INTEGER
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
            symbol_id UNINDEXED,
            name_tokens,
            qualified_tokens,
            file_path_tokens,
            signature_tokens,
            docstring_tokens,
            snippet_tokens,
            tokenize='unicode61'
        );
        """
    )


class SQLiteIndexStore:
    """基于 SQLite WAL + FTS5 的增量项目索引与混合代码检索存储。"""

    def __init__(
        self,
        workspace: Workspace,
        *,
        base_dir: str | Path | None = None,
        db_path: str | Path | None = None,
        adapters: Sequence[LanguageAdapter] | None = None,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        self.workspace = workspace
        if db_path is not None:
            self._db_path = Path(db_path).expanduser()
        else:
            env_root = os.environ.get("MINI_HARNESS_EXPLORER_HOME")
            root = (
                Path(base_dir).expanduser()
                if base_dir is not None
                else (
                    Path(env_root).expanduser()
                    if env_root
                    else DEFAULT_INDEX_ROOT
                )
            )
            self._db_path = root / workspace.id / "index.db"
        self.adapters = adapters
        self.max_file_bytes = max_file_bytes

    @property
    def db_path(self) -> Path:
        return self._db_path

    def exists(self) -> bool:
        return self._db_path.is_file()

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=10.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA temp_store = MEMORY;")
        _init_schema(conn)
        self._ensure_compatible_meta(conn)
        return conn

    def _ensure_compatible_meta(self, conn: sqlite3.Connection) -> None:
        rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        if not rows:
            return
        stored_version = rows.get("sqlite_schema_version")
        stored_workspace = rows.get("workspace_id")
        if (
            stored_version != str(SQLITE_SCHEMA_VERSION)
            or stored_workspace != self.workspace.id
        ):
            # 架构版本或 workspace 不一致时安全重建表内容
            conn.executescript(
                """
                DELETE FROM files;
                DELETE FROM symbols;
                DELETE FROM imports;
                DELETE FROM calls;
                DELETE FROM diagnostics;
                DELETE FROM edges;
                DELETE FROM unresolved;
                DELETE FROM symbols_fts;
                DELETE FROM meta;
                """
            )

    def sync_incremental(
        self,
        *,
        scanner: ProjectScanner | None = None,
        rebuild_edges: bool = True,
    ) -> SyncStats:
        """增量扫描 Workspace 并原子同步至 SQLite（无需返回完整内存图）。"""
        stats, _, _ = self._sync_core(
            scanner=scanner,
            rebuild_edges=rebuild_edges,
            load_result=False,
        )
        return stats

    def sync(
        self,
        *,
        scanner: ProjectScanner | None = None,
    ) -> IncrementalSyncResult:
        """增量同步并返回完整的 ``ProjectIndex`` 与 ``CodeGraph``。"""
        stats, index, graph = self._sync_core(
            scanner=scanner,
            rebuild_edges=True,
            load_result=True,
        )
        assert index is not None and graph is not None
        return IncrementalSyncResult(index=index, graph=graph, stats=stats)

    def _sync_core(
        self,
        *,
        scanner: ProjectScanner | None,
        rebuild_edges: bool,
        load_result: bool,
    ) -> tuple[SyncStats, ProjectIndex | None, CodeGraph | None]:
        """单连接、单次加载的增量同步核心实现。"""
        started = time.perf_counter()
        active_scanner = scanner or ProjectScanner(
            self.workspace,
            adapters=self.adapters,
            max_file_bytes=self.max_file_bytes,
        )

        loaded_index: ProjectIndex | None = None
        loaded_graph: CodeGraph | None = None

        with self._connect() as conn:
            existing: dict[str, tuple[int, int, str, str]] = {
                row[0]: (row[1], row[2], row[3], row[4])
                for row in conn.execute(
                    "SELECT path, size, mtime_ns, sha256, parse_status FROM files"
                ).fetchall()
            }
            discovered_paths: set[str] = set()
            changed_entries: list[
                tuple[FileRecord, int, ParsedFile | None, list[str]]
            ] = []
            mtime_only_updates: list[tuple[int, str]] = []
            unchanged = 0
            added = 0
            updated = 0

            for path in active_scanner.iter_source_files():
                relative_path = path.relative_to(self.workspace.path).as_posix()
                discovered_paths.add(relative_path)
                adapter = active_scanner.adapter_for(path)
                if adapter is None:  # pragma: no cover
                    continue

                if path.is_symlink():
                    file_record, parsed = active_scanner._scan_file(
                        path, relative_path, adapter
                    )
                    if relative_path in existing:
                        updated += 1
                    else:
                        added += 1
                    changed_entries.append((file_record, 0, parsed, []))
                    continue

                try:
                    stat = path.stat()
                except OSError:
                    file_record, parsed = active_scanner._scan_file(
                        path, relative_path, adapter
                    )
                    if relative_path in existing:
                        updated += 1
                    else:
                        added += 1
                    changed_entries.append((file_record, 0, parsed, []))
                    continue

                prev = existing.get(relative_path)
                if (
                    prev is not None
                    and prev[0] == stat.st_size
                    and prev[1] == stat.st_mtime_ns
                ):
                    unchanged += 1
                    continue

                # mtime 或 size 变化时先校验 sha256，内容未变则只更新 mtime_ns
                file_record, parsed, source_lines, content_unchanged = (
                    self._parse_if_content_changed(
                        active_scanner,
                        path,
                        relative_path,
                        adapter,
                        stat_size=stat.st_size,
                        prev_sha256=(
                            prev[2] if prev is not None and prev[3] == "ok" else None
                        ),
                    )
                )
                if content_unchanged:
                    mtime_only_updates.append((stat.st_mtime_ns, relative_path))
                    unchanged += 1
                    continue

                if prev is not None:
                    updated += 1
                else:
                    added += 1
                changed_entries.append(
                    (file_record, stat.st_mtime_ns, parsed, source_lines)
                )

            deleted_paths = sorted(set(existing) - discovered_paths)
            edges_rebuilt = False

            if mtime_only_updates:
                conn.executemany(
                    "UPDATE files SET mtime_ns = ? WHERE path = ?",
                    mtime_only_updates,
                )

            dirty_paths = [entry[0].path for entry in changed_entries] + deleted_paths
            if dirty_paths:
                self._delete_files_data(conn, dirty_paths)
                self._insert_files_data(conn, changed_entries)

            has_edges = (
                conn.execute("SELECT 1 FROM edges LIMIT 1").fetchone() is not None
            )
            has_symbols = (
                conn.execute("SELECT 1 FROM symbols LIMIT 1").fetchone() is not None
            )
            if rebuild_edges and (dirty_paths or (has_symbols and not has_edges)):
                loaded_index = self._load_index_from_conn(conn)
                loaded_graph = build_code_graph(loaded_index)
                self._replace_graph_edges(conn, loaded_graph)
                edges_rebuilt = True

            if dirty_paths or not existing or edges_rebuilt:
                self._write_meta(conn)

            conn.commit()

            if load_result:
                if loaded_index is None:
                    loaded_index = self._load_index_from_conn(conn)
                if loaded_graph is None:
                    loaded_graph = self._load_graph_from_conn(conn, loaded_index)

        elapsed_ms = (time.perf_counter() - started) * 1000
        stats = SyncStats(
            total_files=len(discovered_paths),
            unchanged_files=unchanged,
            reparsed_files=len(changed_entries),
            added_files=added,
            updated_files=updated,
            deleted_files=len(deleted_paths),
            edges_rebuilt=edges_rebuilt,
            duration_ms=elapsed_ms,
        )
        return stats, loaded_index, loaded_graph

    def load_index(self) -> ProjectIndex | None:
        """直接从 SQLite 读取当前已持久化索引（不扫描磁盘）。"""
        if not self.exists():
            return None
        with self._connect() as conn:
            meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
            if not meta:
                return None
            return self._load_index_from_conn(conn)

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        kind: str | None = None,
        path_prefix: str | None = None,
        auto_sync: bool = False,
    ) -> list[CodeSearchHit]:
        """执行 FTS5 BM25 + 查询覆盖率 + CodeGraph 结构中心性 RRF 混合检索。"""
        cleaned = query.strip()
        if not cleaned:
            return []
        if auto_sync or not self.exists():
            self.sync_incremental(rebuild_edges=True)

        limit = max(1, min(int(limit), 50))
        groups = _extract_query_groups(cleaned)
        if not groups:
            return []

        flat_tokens: list[str] = []
        for group in groups:
            for tok in group:
                if tok not in flat_tokens:
                    flat_tokens.append(tok)
        if not flat_tokens:
            return []

        fts_expr = " OR ".join(_quote_fts_token(tok) for tok in flat_tokens[:32])
        candidate_limit = max(limit * 8, 60)

        where_clauses = ["symbols_fts MATCH ?"]
        params: list[Any] = [fts_expr]
        if kind:
            where_clauses.append("s.kind = ?")
            params.append(kind)
        if path_prefix:
            normalized_prefix = path_prefix.strip().strip("/")
            if normalized_prefix:
                where_clauses.append(
                    "(s.file_path = ? OR s.file_path LIKE ?)"
                )
                params.append(normalized_prefix)
                params.append(f"{normalized_prefix}/%")

        where_sql = " AND ".join(where_clauses)
        # bm25 列权重: symbol_id(0), name(12), qualified(7), file_path(5),
        # signature(5), docstring(9), snippet(2.5)
        sql = f"""
            SELECT
                s.id,
                s.kind,
                s.name,
                s.qualified_name,
                s.file_path,
                s.start_line,
                s.end_line,
                s.signature,
                s.docstring,
                s.language,
                s.snippet,
                bm25(symbols_fts, 0.0, 12.0, 7.0, 5.0, 5.0, 9.0, 2.5) AS bm25_score
            FROM symbols_fts
            JOIN symbols AS s ON s.id = symbols_fts.symbol_id
            WHERE {where_sql}
            ORDER BY bm25_score ASC
            LIMIT ?
        """
        params.append(candidate_limit)

        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            if not rows:
                return []

            symbol_ids = [row[0] for row in rows]
            in_degrees, out_degrees = self._fetch_degrees(conn, symbol_ids)

            # 计算图中心度排名（供 RRF 融合）
            degree_sorted = sorted(
                symbol_ids,
                key=lambda sid: (
                    -(in_degrees.get(sid, 0) * 2 + out_degrees.get(sid, 0)),
                    sid,
                ),
            )
            degree_rank_map = {
                sid: rank for rank, sid in enumerate(degree_sorted, start=1)
            }

            query_lower = cleaned.lower()
            query_mentions_test = ("test" in query_lower) or ("测试" in cleaned)
            query_mentions_script = (
                ("script" in query_lower)
                or ("benchmark" in query_lower)
                or ("基准" in cleaned)
            )

            scored: list[tuple[float, int, tuple[Any, ...], int]] = []
            rrf_k = 30.0
            for bm25_rank, row in enumerate(rows, start=1):
                (
                    sid,
                    s_kind,
                    name,
                    qname,
                    fpath,
                    _start,
                    _end,
                    signature,
                    docstring,
                    _lang,
                    snippet,
                    _bm25_raw,
                ) = row

                # 统计命中的语义词组覆盖率
                searchable_blob = tokenize_for_fts(
                    f"{name} {qname} {fpath} {signature or ''} {docstring or ''} {snippet}"
                )
                matched_groups = sum(
                    1
                    for group in groups
                    if any(tok in searchable_blob for tok in group)
                )
                coverage = matched_groups / len(groups)

                rrf_fts = 1.0 / (rrf_k + bm25_rank)
                rrf_graph = 1.0 / (rrf_k + degree_rank_map.get(sid, candidate_limit))

                # 精确标识符/路径加分
                exact_bonus = 0.0
                name_lower = name.lower()
                if name_lower == query_lower:
                    exact_bonus += 0.045
                elif any(name_lower == tok for tok in flat_tokens if len(tok) >= 3):
                    exact_bonus += 0.022
                if s_kind in ("class", "function", "method"):
                    exact_bonus += 0.004

                combined = (
                    rrf_fts * 1.4
                    + (coverage * 0.035)
                    + (rrf_graph * 0.25)
                    + exact_bonus
                )

                # 默认适度降权 tests/ 与 scripts/，让生产核心代码优先浮现
                if not query_mentions_test and (
                    fpath.startswith("tests/") or "/tests/" in fpath
                ):
                    combined *= 0.65
                if not query_mentions_script and fpath.startswith("scripts/"):
                    combined *= 0.80

                scored.append((combined, bm25_rank, row, matched_groups))

            scored.sort(key=lambda item: (-item[0], item[1]))

            # 同文件多样性重排：非单文件过滤时，避免同一个文件里的 module/class/config/result
            # 连续霸榜挤掉跨文件关联核心模块（如 dream.py 挤掉 store.py / scheduler.py）。
            single_file_scope = bool(
                path_prefix
                and path_prefix.strip().endswith(
                    (
                        ".py",
                        ".ts",
                        ".tsx",
                        ".mts",
                        ".cts",
                        ".js",
                        ".jsx",
                        ".mjs",
                        ".cjs",
                        ".java",
                    )
                )
            )
            if not single_file_scope:
                diversified: list[tuple[float, int, tuple[Any, ...], int]] = []
                file_seen: dict[str, int] = {}
                for score, bm25_rank, row, matched_groups in scored:
                    fpath = row[4]
                    count = file_seen.get(fpath, 0)
                    file_seen[fpath] = count + 1
                    # 同文件第 1 条原分保留，第 2 条 0.84x，第 3 条起 0.65x
                    decay = 1.0 if count == 0 else (0.84 if count == 1 else 0.65)
                    diversified.append(
                        (score * decay, bm25_rank, row, matched_groups)
                    )
                diversified.sort(key=lambda item: (-item[0], item[1]))
                top_items = diversified[:limit]
            else:
                top_items = scored[:limit]

            top_ids = [item[2][0] for item in top_items]
            callers_map, callees_map = self._fetch_neighbors(conn, top_ids)

        hits: list[CodeSearchHit] = []
        for score, bm25_rank, row, matched_groups in top_items:
            (
                sid,
                s_kind,
                name,
                qname,
                fpath,
                start_line,
                end_line,
                signature,
                docstring,
                lang,
                _snippet,
                _bm25_raw,
            ) = row
            hits.append(
                CodeSearchHit(
                    symbol_id=sid,
                    kind=s_kind,
                    name=name,
                    qualified_name=qname,
                    file_path=fpath,
                    start_line=start_line,
                    end_line=end_line,
                    signature=signature,
                    docstring=docstring,
                    language=lang,
                    score=score,
                    bm25_rank=bm25_rank,
                    in_degree=in_degrees.get(sid, 0),
                    out_degree=out_degrees.get(sid, 0),
                    matched_terms=matched_groups,
                    total_terms=len(groups),
                    callers=callers_map.get(sid, []),
                    callees=callees_map.get(sid, []),
                )
            )
        return hits

    def _parse_if_content_changed(
        self,
        scanner: ProjectScanner,
        path: Path,
        relative_path: str,
        adapter: LanguageAdapter,
        *,
        stat_size: int,
        prev_sha256: str | None,
    ) -> tuple[FileRecord, ParsedFile | None, list[str], bool]:
        """读取文件并校验 SHA-256；仅当内容真正变化时执行语言解析。"""
        try:
            self.workspace.resolve_path(path)
        except (WorkspacePathError, WorkspaceError) as exc:
            record, parsed = scanner._skipped_file(
                relative_path,
                adapter.language,
                "path_outside_workspace",
                f"跳过 Workspace 外路径：{exc}",
            )
            return record, parsed, [], False

        if stat_size > scanner.max_file_bytes:
            record, parsed = scanner._skipped_file(
                relative_path,
                adapter.language,
                "file_too_large",
                (
                    f"跳过超过大小限制的文件：{stat_size} bytes "
                    f"> {scanner.max_file_bytes} bytes"
                ),
                size=stat_size,
                severity="warning",
            )
            return record, parsed, [], False

        try:
            raw = path.read_bytes()
        except OSError as exc:
            record, parsed = scanner._error_file(
                relative_path,
                adapter.language,
                "read_error",
                f"无法读取文件：{exc}",
                size=stat_size,
            )
            return record, parsed, [], False

        if len(raw) > scanner.max_file_bytes:
            record, parsed = scanner._skipped_file(
                relative_path,
                adapter.language,
                "file_too_large",
                "文件在读取期间超过大小限制。",
                size=len(raw),
                severity="warning",
            )
            return record, parsed, [], False

        sha256 = hashlib.sha256(raw).hexdigest()
        if prev_sha256 is not None and sha256 == prev_sha256:
            dummy = FileRecord(
                path=relative_path,
                language=adapter.language,
                size=len(raw),
                sha256=sha256,
                line_count=0,
                parse_status="ok",
            )
            return dummy, None, [], True

        try:
            text = _decode_source(raw)
        except ValueError as exc:
            record, parsed = scanner._error_file(
                relative_path,
                adapter.language,
                "decode_error",
                str(exc),
                size=len(raw),
            )
            return record, parsed, [], False

        lines = text.splitlines()
        source = SourceFile(
            path=str(path),
            relative_path=relative_path,
            size=len(raw),
            sha256=sha256,
            text=text,
            line_count=len(lines),
        )
        try:
            parsed = adapter.parse(source)
        except Exception as exc:  # noqa: BLE001
            record, parsed_err = scanner._error_file(
                relative_path,
                adapter.language,
                "adapter_error",
                f"语言适配器解析失败：{exc}",
                size=len(raw),
                sha256=sha256,
                line_count=len(lines),
            )
            return record, parsed_err, [], False

        has_error = any(item.severity == "error" for item in parsed.diagnostics)
        first_err = next(
            (item.message for item in parsed.diagnostics if item.severity == "error"),
            None,
        )
        record = FileRecord(
            path=relative_path,
            language=adapter.language,
            size=len(raw),
            sha256=sha256,
            line_count=len(lines),
            parse_status="error" if has_error else "ok",
            error=first_err if has_error else None,
        )
        return record, parsed, lines, False

    def _delete_files_data(
        self, conn: sqlite3.Connection, file_paths: Sequence[str]
    ) -> None:
        chunk_size = 400
        for i in range(0, len(file_paths), chunk_size):
            chunk = list(file_paths[i : i + chunk_size])
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM symbols_fts WHERE symbol_id IN "
                f"(SELECT id FROM symbols WHERE file_path IN ({placeholders}))",
                chunk,
            )
            conn.execute(
                f"DELETE FROM symbols WHERE file_path IN ({placeholders})", chunk
            )
            conn.execute(
                f"DELETE FROM imports WHERE file_path IN ({placeholders})", chunk
            )
            conn.execute(
                f"DELETE FROM calls WHERE file_path IN ({placeholders})", chunk
            )
            conn.execute(
                f"DELETE FROM diagnostics WHERE file_path IN ({placeholders})", chunk
            )
            conn.execute(
                f"DELETE FROM files WHERE path IN ({placeholders})", chunk
            )

    def _insert_files_data(
        self,
        conn: sqlite3.Connection,
        entries: Sequence[tuple[FileRecord, int, ParsedFile | None, list[str]]],
    ) -> None:
        file_rows: list[tuple[Any, ...]] = []
        symbol_rows: list[tuple[Any, ...]] = []
        fts_rows: list[tuple[Any, ...]] = []
        import_rows: list[tuple[Any, ...]] = []
        call_rows: list[tuple[Any, ...]] = []
        diag_rows: list[tuple[Any, ...]] = []

        for file_record, mtime_ns, parsed, lines in entries:
            file_rows.append(
                (
                    file_record.path,
                    file_record.language,
                    file_record.size,
                    mtime_ns,
                    file_record.sha256,
                    file_record.line_count,
                    file_record.parse_status,
                    file_record.error,
                )
            )
            if parsed is None:
                continue
            for sym in parsed.symbols:
                snippet = _extract_symbol_snippet(lines, sym)
                symbol_rows.append(
                    (
                        sym.id,
                        sym.kind,
                        sym.name,
                        sym.qualified_name,
                        sym.file_path,
                        sym.start_line,
                        sym.end_line,
                        sym.signature,
                        sym.docstring,
                        json.dumps(sym.decorators, ensure_ascii=False)
                        if sym.decorators
                        else "",
                        json.dumps(sym.bases, ensure_ascii=False)
                        if sym.bases
                        else "",
                        sym.parent_id,
                        sym.language,
                        snippet,
                    )
                )
                fts_rows.append(
                    (
                        sym.id,
                        tokenize_for_fts(sym.name),
                        tokenize_for_fts(sym.qualified_name),
                        tokenize_for_fts(sym.file_path),
                        tokenize_for_fts(sym.signature),
                        tokenize_for_fts(sym.docstring),
                        tokenize_for_fts(snippet),
                    )
                )
            for imp in parsed.imports:
                import_rows.append(
                    (
                        imp.file_path,
                        imp.module,
                        imp.name,
                        imp.alias,
                        imp.level,
                        imp.line,
                        imp.column,
                    )
                )
            for call in parsed.calls:
                call_rows.append(
                    (
                        file_record.path,
                        call.caller_id,
                        call.callee_expression,
                        call.callee_name,
                        call.line,
                        call.column,
                    )
                )
            for diag in parsed.diagnostics:
                diag_rows.append(
                    (
                        diag.file_path or file_record.path,
                        diag.code,
                        diag.message,
                        diag.severity,
                        diag.line,
                        diag.column,
                    )
                )

        if file_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO files "
                "(path, language, size, mtime_ns, sha256, line_count, parse_status, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                file_rows,
            )
        if symbol_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO symbols "
                "(id, kind, name, qualified_name, file_path, start_line, end_line, "
                "signature, docstring, decorators_json, bases_json, parent_id, language, snippet) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                symbol_rows,
            )
        if fts_rows:
            conn.executemany(
                "INSERT INTO symbols_fts "
                "(symbol_id, name_tokens, qualified_tokens, file_path_tokens, "
                "signature_tokens, docstring_tokens, snippet_tokens) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                fts_rows,
            )
        if import_rows:
            conn.executemany(
                "INSERT INTO imports (file_path, module, name, alias, level, line, col) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                import_rows,
            )
        if call_rows:
            conn.executemany(
                "INSERT INTO calls (file_path, caller_id, callee_expression, callee_name, line, col) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                call_rows,
            )
        if diag_rows:
            conn.executemany(
                "INSERT INTO diagnostics (file_path, code, message, severity, line, col) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                diag_rows,
            )

    def _replace_graph_edges(
        self, conn: sqlite3.Connection, graph: CodeGraph
    ) -> None:
        conn.execute("DROP INDEX IF EXISTS idx_edges_source")
        conn.execute("DROP INDEX IF EXISTS idx_edges_target")
        conn.execute("DELETE FROM edges")
        conn.execute("DELETE FROM unresolved")
        if graph.edges:
            conn.executemany(
                "INSERT INTO edges "
                "(id, source, target, kind, confidence, line, detail, occurrences) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        edge.id,
                        edge.source,
                        edge.target,
                        edge.kind,
                        edge.confidence,
                        edge.line,
                        edge.detail,
                        edge.occurrences,
                    )
                    for edge in graph.edges
                ],
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target)")
        if graph.unresolved:
            conn.executemany(
                "INSERT INTO unresolved (kind, source_id, expression, reason, line) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        item.kind,
                        item.source_id,
                        item.expression,
                        item.reason,
                        item.line,
                    )
                    for item in graph.unresolved
                ],
            )

    def _write_meta(self, conn: sqlite3.Connection) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [
                ("sqlite_schema_version", str(SQLITE_SCHEMA_VERSION)),
                ("index_schema_version", str(SCHEMA_VERSION)),
                ("workspace_root", str(self.workspace.path)),
                ("workspace_id", self.workspace.id),
                ("generated_at", now),
            ],
        )

    def _load_index_from_conn(self, conn: sqlite3.Connection) -> ProjectIndex:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        files = [
            FileRecord(
                path=row[0],
                language=row[1],
                size=row[2],
                sha256=row[3],
                line_count=row[4],
                parse_status=row[5],
                error=row[6],
            )
            for row in conn.execute(
                "SELECT path, language, size, sha256, line_count, parse_status, error "
                "FROM files ORDER BY path"
            ).fetchall()
        ]
        symbols = [
            SymbolRecord(
                id=row[0],
                kind=row[1],
                name=row[2],
                qualified_name=row[3],
                file_path=row[4],
                start_line=row[5],
                end_line=row[6],
                signature=row[7],
                docstring=row[8],
                decorators=json.loads(row[9]) if row[9] else [],
                bases=json.loads(row[10]) if row[10] else [],
                parent_id=row[11],
                language=row[12],
            )
            for row in conn.execute(
                "SELECT id, kind, name, qualified_name, file_path, start_line, end_line, "
                "signature, docstring, decorators_json, bases_json, parent_id, language "
                "FROM symbols ORDER BY file_path, start_line, id"
            ).fetchall()
        ]
        imports = [
            ImportRecord(
                file_path=row[0],
                module=row[1],
                name=row[2],
                alias=row[3],
                level=row[4],
                line=row[5],
                column=row[6],
            )
            for row in conn.execute(
                "SELECT file_path, module, name, alias, level, line, col "
                "FROM imports ORDER BY id"
            ).fetchall()
        ]
        calls = [
            CallRecord(
                caller_id=row[0],
                callee_expression=row[1],
                callee_name=row[2],
                line=row[3],
                column=row[4],
            )
            for row in conn.execute(
                "SELECT caller_id, callee_expression, callee_name, line, col "
                "FROM calls ORDER BY id"
            ).fetchall()
        ]
        diagnostics = [
            DiagnosticRecord(
                code=row[0],
                message=row[1],
                severity=row[2],
                file_path=row[3],
                line=row[4],
                column=row[5],
            )
            for row in conn.execute(
                "SELECT code, message, severity, file_path, line, col "
                "FROM diagnostics ORDER BY id"
            ).fetchall()
        ]
        return ProjectIndex(
            schema_version=SCHEMA_VERSION,
            workspace_root=meta.get("workspace_root", str(self.workspace.path)),
            workspace_id=meta.get("workspace_id", self.workspace.id),
            generated_at=meta.get(
                "generated_at", datetime.now(timezone.utc).isoformat()
            ),
            files=files,
            symbols=symbols,
            imports=imports,
            calls=calls,
            diagnostics=diagnostics,
        )

    def _load_graph_from_conn(
        self, conn: sqlite3.Connection, index: ProjectIndex
    ) -> CodeGraph:
        edges = [
            GraphEdge(
                id=row[0],
                source=row[1],
                target=row[2],
                kind=row[3],
                confidence=row[4],
                line=row[5],
                detail=row[6],
                occurrences=row[7],
            )
            for row in conn.execute(
                "SELECT id, source, target, kind, confidence, line, detail, occurrences "
                "FROM edges"
            ).fetchall()
        ]
        unresolved = [
            UnresolvedReference(
                kind=row[0],
                source_id=row[1],
                expression=row[2],
                reason=row[3],
                line=row[4],
            )
            for row in conn.execute(
                "SELECT kind, source_id, expression, reason, line FROM unresolved"
            ).fetchall()
        ]
        return CodeGraph(
            workspace_root=index.workspace_root,
            workspace_id=index.workspace_id,
            nodes=index.symbols,
            edges=edges,
            unresolved=unresolved,
        )

    def _fetch_degrees(
        self, conn: sqlite3.Connection, symbol_ids: Sequence[str]
    ) -> tuple[dict[str, int], dict[str, int]]:
        if not symbol_ids:
            return {}, {}
        placeholders = ",".join("?" for _ in symbol_ids)
        in_deg = {
            row[0]: int(row[1])
            for row in conn.execute(
                f"SELECT target, COUNT(*) FROM edges "
                f"WHERE kind IN ('calls', 'imports') AND target IN ({placeholders}) "
                f"GROUP BY target",
                list(symbol_ids),
            ).fetchall()
        }
        out_deg = {
            row[0]: int(row[1])
            for row in conn.execute(
                f"SELECT source, COUNT(*) FROM edges "
                f"WHERE kind IN ('calls', 'imports') AND source IN ({placeholders}) "
                f"GROUP BY source",
                list(symbol_ids),
            ).fetchall()
        }
        return in_deg, out_deg

    def _fetch_neighbors(
        self, conn: sqlite3.Connection, symbol_ids: Sequence[str]
    ) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        if not symbol_ids:
            return {}, {}
        placeholders = ",".join("?" for _ in symbol_ids)
        callers: dict[str, list[str]] = {sid: [] for sid in symbol_ids}
        callees: dict[str, list[str]] = {sid: [] for sid in symbol_ids}

        for target_id, caller_qname in conn.execute(
            f"SELECT e.target, s.qualified_name FROM edges AS e "
            f"JOIN symbols AS s ON s.id = e.source "
            f"WHERE e.kind = 'calls' AND e.target IN ({placeholders}) "
            f"ORDER BY e.occurrences DESC, s.qualified_name ASC",
            list(symbol_ids),
        ).fetchall():
            bucket = callers.get(target_id)
            if bucket is not None and len(bucket) < 3 and caller_qname not in bucket:
                bucket.append(caller_qname)

        for source_id, callee_qname in conn.execute(
            f"SELECT e.source, s.qualified_name FROM edges AS e "
            f"JOIN symbols AS s ON s.id = e.target "
            f"WHERE e.kind = 'calls' AND e.source IN ({placeholders}) "
            f"ORDER BY e.occurrences DESC, s.qualified_name ASC",
            list(symbol_ids),
        ).fetchall():
            bucket = callees.get(source_id)
            if bucket is not None and len(bucket) < 3 and callee_qname not in bucket:
                bucket.append(callee_qname)

        return callers, callees


__all__ = [
    "SQLITE_SCHEMA_VERSION",
    "CodeSearchHit",
    "IncrementalSyncResult",
    "SQLiteIndexError",
    "SQLiteIndexStore",
    "SyncStats",
    "tokenize_for_fts",
]
