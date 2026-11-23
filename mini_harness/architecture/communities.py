"""Phase 4：基于 CodeGraph 的确定性模块社区聚类（Community Detection）。

面向中大型仓库，将数百个模块先按依赖紧密度（``imports`` + 跨模块 ``calls`` +
目录内聚）通过确定性加权标签传播（Weighted Label Propagation）聚合成 5~12 个
高内聚逻辑子系统（Community），再把社区摘要与跨社区链路提供给 LLM 架构分析器，
显著压降 Prompt Token 并提升高层组件划分质量。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from mini_harness.explorer import CodeGraph, SymbolRecord

_NOISE_TOP_DIRS = frozenset(
    {"tests", "test", "scripts", "script", "benchmarks", "benchmark", "examples"}
)


def is_noise_path(file_path: str) -> bool:
    """判断路径是否属于测试、离线脚本或示例噪声目录。"""
    parts = PurePosixPath(file_path).parts
    if not parts:
        return False
    if parts[0].lower() in _NOISE_TOP_DIRS:
        return True
    name = parts[-1].lower()
    if name.startswith("test_") or name.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts")):
        return True
    return False


@dataclass(frozen=True, slots=True)
class CommunityLink:
    """两个社区之间的聚合依赖边。"""

    target_id: str
    target_name: str
    imports: int = 0
    calls: int = 0

    @property
    def total(self) -> int:
        return self.imports + self.calls

    def to_dict(self) -> dict[str, Any]:
        return {
            "targetId": self.target_id,
            "targetName": self.target_name,
            "imports": self.imports,
            "calls": self.calls,
        }


@dataclass(frozen=True, slots=True)
class ArchitectureCommunity:
    """由图聚类得到的一个逻辑子系统摘要。"""

    id: str
    name: str
    module_count: int
    symbol_count: int
    modules: list[str] = field(default_factory=list)
    hub_symbols: list[dict[str, Any]] = field(default_factory=list)
    internal_edges: int = 0
    external_links: list[CommunityLink] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "moduleCount": self.module_count,
            "symbolCount": self.symbol_count,
            "modules": list(self.modules),
            "hubSymbols": list(self.hub_symbols),
            "internalEdges": self.internal_edges,
            "externalLinks": [link.to_dict() for link in self.external_links],
        }


def _directory_group(file_path: str) -> str:
    parts = PurePosixPath(file_path).parts
    if len(parts) <= 1:
        return parts[0] if parts else "root"
    # 最多取前 3 级目录，保证 frontend/src/components 与 mini_harness/agent 等可读分组
    depth = min(len(parts) - 1, 3)
    return "/".join(parts[:depth])


def _symbol_module_id(
    graph: CodeGraph,
    symbol_id: str,
    module_by_file: dict[str, str],
) -> str | None:
    node = graph.get_node(symbol_id)
    if node is None:
        return None
    if node.kind == "module":
        return node.id
    return module_by_file.get(node.file_path)


def detect_architecture_communities(
    graph: CodeGraph,
    *,
    max_communities: int = 12,
    exclude_noise: bool = True,
    max_iterations: int = 20,
) -> list[ArchitectureCommunity]:
    """对 ``CodeGraph`` 执行确定性加权标签传播，输出按规模排序的子系统列表。"""
    all_modules = [n for n in graph.node_list if n.kind == "module"]
    if not all_modules:
        return []

    prod_modules = (
        [m for m in all_modules if not is_noise_path(m.file_path)]
        if exclude_noise
        else list(all_modules)
    )
    active_modules = prod_modules if prod_modules else all_modules
    active_ids = {m.id for m in active_modules}
    module_by_id = {m.id: m for m in active_modules}
    module_by_file = {m.file_path: m.id for m in active_modules}

    # 构建无向加权模块邻接表 + 统计有向跨模块依赖
    adj: dict[str, dict[str, float]] = {m.id: defaultdict(float) for m in active_modules}
    directed_imports: dict[tuple[str, str], int] = defaultdict(int)
    directed_calls: dict[tuple[str, str], int] = defaultdict(int)

    for edge in graph.edges:
        if edge.kind not in ("imports", "calls"):
            continue
        src_mod = _symbol_module_id(graph, edge.source, module_by_file)
        tgt_mod = _symbol_module_id(graph, edge.target, module_by_file)
        if (
            src_mod is None
            or tgt_mod is None
            or src_mod == tgt_mod
            or src_mod not in active_ids
            or tgt_mod not in active_ids
        ):
            continue
        if edge.kind == "imports":
            weight = 2.5
            directed_imports[(src_mod, tgt_mod)] += edge.occurrences
        else:
            weight = 1.0 + min(3.0, 0.4 * edge.occurrences)
            directed_calls[(src_mod, tgt_mod)] += edge.occurrences
        adj[src_mod][tgt_mod] += weight
        adj[tgt_mod][src_mod] += weight

    # 同目录轻度内聚边：确保同一子包内弱连接的模块不会碎成孤岛
    by_dir: dict[str, list[str]] = defaultdict(list)
    for mod in active_modules:
        by_dir[_directory_group(mod.file_path)].append(mod.id)
    for group_ids in by_dir.values():
        if len(group_ids) < 2:
            continue
        sorted_ids = sorted(group_ids)
        # 形成轻度环状内聚，避免 O(n^2) 稠密边覆盖真实跨模块依赖
        for idx, mod_id in enumerate(sorted_ids):
            neighbor = sorted_ids[(idx + 1) % len(sorted_ids)]
            adj[mod_id][neighbor] += 1.2
            adj[neighbor][mod_id] += 1.2

    # 确定性加权标签传播（初始标签按目录分组，避免单文件漂移）
    labels: dict[str, str] = {
        m.id: _directory_group(m.file_path) for m in active_modules
    }
    ordered_ids = sorted(active_ids, key=lambda mid: module_by_id[mid].file_path)

    for _ in range(max_iterations):
        changed = False
        for mod_id in ordered_ids:
            neighbors = adj[mod_id]
            if not neighbors:
                continue
            score_by_label: dict[str, float] = defaultdict(float)
            # 保留自身目录先验，防止大枢纽模块把整个仓库吞成 1 个巨型社区
            own_dir = _directory_group(module_by_id[mod_id].file_path)
            score_by_label[own_dir] += 2.0
            for nb_id, weight in neighbors.items():
                score_by_label[labels[nb_id]] += weight
            best_label = min(
                score_by_label.items(),
                key=lambda item: (-item[1], item[0]),
            )[0]
            if best_label != labels[mod_id]:
                labels[mod_id] = best_label
                changed = True
        if not changed:
            break

    groups: dict[str, list[SymbolRecord]] = defaultdict(list)
    for mod_id in ordered_ids:
        groups[labels[mod_id]].append(module_by_id[mod_id])

    # 如果社区数超出上限，将较小的社区按顶层目录归并
    if len(groups) > max_communities:
        sorted_groups = sorted(
            groups.items(),
            key=lambda kv: (-len(kv[1]), kv[0]),
        )
        kept = dict(sorted_groups[: max_communities - 1])
        overflow_mods: list[SymbolRecord] = []
        for _, mods in sorted_groups[max_communities - 1 :]:
            overflow_mods.extend(mods)
        if overflow_mods:
            kept["other"] = overflow_mods
        groups = kept

    # 统计每个模块内的符号数与枢纽符号
    symbols_by_mod: dict[str, list[SymbolRecord]] = defaultdict(list)
    for node in graph.node_list:
        if node.kind == "module":
            continue
        mod_id = module_by_file.get(node.file_path)
        if mod_id is not None:
            symbols_by_mod[mod_id].append(node)

    mod_to_comm_id: dict[str, str] = {}
    comm_name_by_id: dict[str, str] = {}
    ordered_groups = sorted(
        groups.items(),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )
    for idx, (label, mods) in enumerate(ordered_groups, start=1):
        slug = label.replace("/", "-").replace(".", "-").strip("-") or f"c{idx}"
        comm_id = f"community:{slug}"
        comm_name_by_id[comm_id] = label
        for mod in mods:
            mod_to_comm_id[mod.id] = comm_id

    communities: list[ArchitectureCommunity] = []
    for label, mods in ordered_groups:
        slug = label.replace("/", "-").replace(".", "-").strip("-") or "cluster"
        comm_id = f"community:{slug}"
        mod_ids = {m.id for m in mods}

        comm_symbols: list[SymbolRecord] = []
        for mod in mods:
            comm_symbols.extend(symbols_by_mod.get(mod.id, []))

        # 选出该社区内连接度最高的 Top-4 核心符号
        ranked_hubs = sorted(
            comm_symbols,
            key=lambda sym: (
                -(
                    len(graph.get_incoming(sym.id, kind="calls")) * 3
                    + len(graph.get_outgoing(sym.id, kind="calls"))
                    + (10 if sym.kind == "class" else 4)
                ),
                sym.file_path,
                sym.start_line,
            ),
        )[:4]
        hub_dicts = [
            {
                "symbolId": sym.id,
                "name": sym.name,
                "kind": sym.kind,
                "filePath": sym.file_path,
            }
            for sym in ranked_hubs
        ]

        internal_edges = 0
        ext_imports: dict[str, int] = defaultdict(int)
        ext_calls: dict[str, int] = defaultdict(int)

        for (src_m, tgt_m), count in directed_imports.items():
            if src_m in mod_ids and tgt_m in mod_ids:
                internal_edges += count
            elif src_m in mod_ids:
                tgt_comm = mod_to_comm_id.get(tgt_m)
                if tgt_comm and tgt_comm != comm_id:
                    ext_imports[tgt_comm] += count

        for (src_m, tgt_m), count in directed_calls.items():
            if src_m in mod_ids and tgt_m in mod_ids:
                internal_edges += count
            elif src_m in mod_ids:
                tgt_comm = mod_to_comm_id.get(tgt_m)
                if tgt_comm and tgt_comm != comm_id:
                    ext_calls[tgt_comm] += count

        ext_targets = set(ext_imports) | set(ext_calls)
        links = [
            CommunityLink(
                target_id=tgt_c,
                target_name=comm_name_by_id.get(tgt_c, tgt_c),
                imports=ext_imports.get(tgt_c, 0),
                calls=ext_calls.get(tgt_c, 0),
            )
            for tgt_c in ext_targets
        ]
        links.sort(key=lambda item: (-item.total, item.target_id))

        communities.append(
            ArchitectureCommunity(
                id=comm_id,
                name=label,
                module_count=len(mods),
                symbol_count=len(comm_symbols),
                modules=[m.file_path for m in sorted(mods, key=lambda x: x.file_path)[:16]],
                hub_symbols=hub_dicts,
                internal_edges=internal_edges,
                external_links=links[:6],
            )
        )

    return communities


__all__ = [
    "ArchitectureCommunity",
    "CommunityLink",
    "detect_architecture_communities",
    "is_noise_path",
]
