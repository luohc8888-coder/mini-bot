import { useCallback, useEffect, useMemo, useState } from "react";
import {
  architectureErrorMessage,
  fetchGitDiff,
  fetchGitStatus,
} from "../architecture/api";
import type {
  GitChangedFile,
  GitDiffResponse,
  GitStatusResponse,
} from "../types/architecture";
import { tokenizeCodeLine } from "../utils/syntaxHighlight";

interface GitDiffModalProps {
  open: boolean;
  onClose: () => void;
  onStatusLoaded?: (status: GitStatusResponse) => void;
  onOpenSymbol: (symbolId: string, openSource: boolean) => void;
}

const STATUS_LABELS: Record<string, string> = {
  M: "已修改",
  A: "已新增",
  D: "已删除",
  R: "重命名",
  "?": "未追踪",
};

export function GitDiffModal({
  open,
  onClose,
  onStatusLoaded,
  onOpenSymbol,
}: GitDiffModalProps) {
  const [status, setStatus] = useState<GitStatusResponse | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [diff, setDiff] = useState<GitDiffResponse | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  const loadStatus = useCallback(
    (signal?: AbortSignal) => {
      setStatusLoading(true);
      setStatusError(null);
      void fetchGitStatus(signal)
        .then((res) => {
          if (signal?.aborted) return;
          setStatus(res);
          onStatusLoaded?.(res);
          setSelectedPath((prev) => {
            if (prev && res.files.some((f) => f.path === prev)) {
              return prev;
            }
            return res.files[0]?.path ?? null;
          });
        })
        .catch((err: unknown) => {
          if (signal?.aborted) return;
          setStatusError(architectureErrorMessage(err));
        })
        .finally(() => {
          if (!signal?.aborted) {
            setStatusLoading(false);
          }
        });
    },
    [onStatusLoaded],
  );

  useEffect(() => {
    if (!open) {
      return;
    }
    const controller = new AbortController();
    loadStatus(controller.signal);
    return () => controller.abort();
  }, [open, refreshTick, loadStatus]);

  useEffect(() => {
    if (!open || !selectedPath) {
      setDiff(null);
      setDiffError(null);
      return;
    }
    const controller = new AbortController();
    setDiffLoading(true);
    setDiffError(null);
    void fetchGitDiff(selectedPath, controller.signal)
      .then((res) => {
        if (!controller.signal.aborted) {
          setDiff(res);
        }
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) {
          setDiffError(architectureErrorMessage(err));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setDiffLoading(false);
        }
      });
    return () => controller.abort();
  }, [open, selectedPath, refreshTick]);

  useEffect(() => {
    if (!open) {
      return;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing) {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  const selectedFile: GitChangedFile | null = useMemo(() => {
    if (!status || !selectedPath) return null;
    return status.files.find((f) => f.path === selectedPath) ?? null;
  }, [status, selectedPath]);

  const highlightedDiffLines = useMemo(() => {
    if (!diff) return [];
    return diff.lines.map((line) => ({
      ...line,
      tokens:
        line.kind === "add" || line.kind === "del" || line.kind === "context"
          ? tokenizeCodeLine(line.text, undefined, diff.filePath)
          : null,
    }));
  }, [diff]);

  if (!open) {
    return null;
  }

  return (
    <div
      className="git-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="只读 Git 变更与 Diff 视图"
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div className="git-modal">
        <header className="git-modal__header">
          <div className="git-modal__title-block">
            <div className="eyebrow">READ-ONLY GIT INSPECTOR</div>
            <div className="git-modal__title-row">
              <h3>Git 工作区变更</h3>
              {status?.available && status.branch ? (
                <span className="git-branch-chip">⎇ {status.branch}</span>
              ) : null}
              {status?.head ? (
                <span
                  className="git-head-chip"
                  title={`${status.head.hash} ${status.head.subject} (${status.head.relativeTime})`}
                >
                  <code>{status.head.hash}</code>
                  <span>{status.head.subject}</span>
                </span>
              ) : null}
            </div>
          </div>
          <div className="git-modal__actions">
            <button
              type="button"
              className="source-drawer__button"
              onClick={() => setRefreshTick((t) => t + 1)}
              disabled={statusLoading}
              title="重新读取 Git 工作区状态"
            >
              {statusLoading ? "刷新中…" : "刷新"}
            </button>
            <button
              type="button"
              className="source-drawer__close"
              onClick={onClose}
              aria-label="关闭 Git 视图"
              title="关闭 (Esc)"
            >
              ×
            </button>
          </div>
        </header>

        {statusError ? (
          <div className="git-modal__empty git-modal__empty--error">
            <strong>无法读取 Git 状态</strong>
            <p>{statusError}</p>
          </div>
        ) : status && !status.available ? (
          <div className="git-modal__empty">
            <strong>当前 Workspace 不是 Git 仓库</strong>
            <p>{status.reason ?? "无法获取 Git 变更信息。"}</p>
          </div>
        ) : status && status.files.length === 0 ? (
          <div className="git-modal__empty">
            <strong>工作区干净，暂无未提交变更</strong>
            <p>
              当前分支 <code>{status.branch ?? "HEAD"}</code>{" "}
              与最新提交保持一致。
            </p>
          </div>
        ) : (
          <div className="git-modal__body">
            <aside className="git-modal__sidebar" aria-label="变更文件列表">
              <div className="git-modal__sidebar-head">
                <span>变更文件（{status?.summary.total ?? 0}）</span>
                {status?.summary ? (
                  <span className="git-summary-mini">
                    {status.summary.modified > 0 ? `M:${status.summary.modified} ` : ""}
                    {status.summary.added > 0 ? `A:${status.summary.added} ` : ""}
                    {status.summary.deleted > 0 ? `D:${status.summary.deleted} ` : ""}
                    {status.summary.untracked > 0 ? `?:${status.summary.untracked}` : ""}
                  </span>
                ) : null}
              </div>
              <div className="git-file-list">
                {(status?.files ?? []).map((item) => {
                  const active = item.path === selectedPath;
                  return (
                    <button
                      key={item.path}
                      type="button"
                      className={`git-file-item${active ? " is-active" : ""}`}
                      onClick={() => setSelectedPath(item.path)}
                      title={`${STATUS_LABELS[item.status] ?? item.status} · ${item.path}`}
                    >
                      <span
                        className={`git-status-badge git-status-badge--${item.category}`}
                      >
                        {item.status}
                      </span>
                      <span className="git-file-item__path">{item.path}</span>
                      {item.symbolCount > 0 ? (
                        <span
                          className="git-file-item__syms"
                          title={`已索引 ${item.symbolCount} 个代码符号`}
                        >
                          {item.symbolCount}
                        </span>
                      ) : null}
                    </button>
                  );
                })}
              </div>
            </aside>

            <section className="git-modal__diff-pane" aria-label="文件 Diff 详情">
              {selectedFile ? (
                <div className="git-diff__toolbar">
                  <div className="git-diff__file-meta">
                    <span
                      className={`git-status-badge git-status-badge--${selectedFile.category}`}
                    >
                      {STATUS_LABELS[selectedFile.status] ?? selectedFile.status}
                    </span>
                    <code className="git-diff__file-path">{selectedFile.path}</code>
                    {diff ? (
                      <span className="git-diff__stats">
                        <span className="git-diff__stat-add">+{diff.additions}</span>
                        <span className="git-diff__stat-del">-{diff.deletions}</span>
                      </span>
                    ) : null}
                  </div>
                  {selectedFile.symbolId ? (
                    <div className="git-diff__actions">
                      <button
                        type="button"
                        className="source-drawer__button"
                        onClick={() => {
                          onClose();
                          onOpenSymbol(selectedFile.symbolId!, false);
                        }}
                        title="在代码结构图中定位此文件"
                      >
                        结构图定位
                      </button>
                      <button
                        type="button"
                        className="source-drawer__button source-drawer__button--active"
                        onClick={() => {
                          onClose();
                          onOpenSymbol(selectedFile.symbolId!, true);
                        }}
                        title="打开源码工作台与 AI 对话"
                      >
                        查看源码与 AI
                      </button>
                    </div>
                  ) : null}
                </div>
              ) : null}

              {diff && diff.symbols.length > 0 ? (
                <div className="git-diff__symbols">
                  <span className="git-diff__symbols-label">跳入符号：</span>
                  {diff.symbols.slice(0, 10).map((sym) => (
                    <button
                      key={sym.symbolId}
                      type="button"
                      className="git-symbol-chip"
                      onClick={() => {
                        onClose();
                        onOpenSymbol(sym.symbolId, true);
                      }}
                      title={`${sym.qualifiedName} (L${sym.startLine}-L${sym.endLine})`}
                    >
                      {sym.name}
                    </button>
                  ))}
                </div>
              ) : null}

              {diffLoading ? (
                <div className="git-modal__empty">
                  <span className="source-drawer__spinner" aria-hidden="true" />
                  <span>正在加载 Diff…</span>
                </div>
              ) : diffError ? (
                <div className="git-modal__empty git-modal__empty--error">
                  <strong>无法读取文件 Diff</strong>
                  <p>{diffError}</p>
                </div>
              ) : diff && highlightedDiffLines.length > 0 ? (
                <div className="git-diff__code">
                  {highlightedDiffLines.map((line, idx) => (
                    <div
                      key={idx}
                      className={`git-diff__line git-diff__line--${line.kind}`}
                    >
                      <span className="git-diff__lno">{line.oldLine ?? ""}</span>
                      <span className="git-diff__lno">{line.newLine ?? ""}</span>
                      <span className="git-diff__prefix">
                        {line.kind === "add"
                          ? "+"
                          : line.kind === "del"
                            ? "-"
                            : line.kind === "context"
                              ? " "
                              : ""}
                      </span>
                      <code>
                        {line.tokens
                          ? line.tokens.map((tok, tIdx) =>
                              tok.kind === "plain" ? (
                                tok.text
                              ) : (
                                <span key={tIdx} className={`tok-${tok.kind}`}>
                                  {tok.text}
                                </span>
                              ),
                            )
                          : line.text || " "}
                      </code>
                    </div>
                  ))}
                  {diff.truncated ? (
                    <div className="git-diff__truncated">
                      Diff 行数超过单次展示上限，已截断尾部内容。
                    </div>
                  ) : null}
                </div>
              ) : (
                <div className="git-modal__empty">
                  <span>该文件暂无文本 Diff 内容（可能仅修改权限或为空变更）。</span>
                </div>
              )}
            </section>
          </div>
        )}

        <footer className="git-modal__footer">
          <span>只读 Git 视图 · 仅执行白名单只读子命令，不修改工作区或暂存区</span>
          <span>Esc 关闭</span>
        </footer>
      </div>
    </div>
  );
}
