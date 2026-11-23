import { useEffect, useMemo, useRef, useState } from "react";
import {
  architectureErrorMessage,
  fetchSourceSnippet,
} from "../architecture/api";
import type {
  ArchitectureSourceRef,
  SourceSnippet,
} from "../types/architecture";

export interface SourceHighlightRange {
  start: number;
  end: number;
  key: number;
}

interface SourceWorkspaceProps {
  sources: ArchitectureSourceRef[];
  activeSymbolId: string | null;
  title?: string;
  highlightRange?: SourceHighlightRange | null;
  onSelectSource: (source: ArchitectureSourceRef) => void;
  onLocateInStructure?: (symbolId: string) => void;
  onClose: () => void;
}

function formatRange(start?: number, end?: number): string {
  if (!start) {
    return "";
  }
  return end && end !== start ? `${start}-${end}` : String(start);
}

function sourceLabel(source: ArchitectureSourceRef): string {
  if (source.symbolName) {
    return source.symbolName;
  }
  if (source.qualifiedName) {
    const parts = source.qualifiedName.split(".").filter(Boolean);
    return parts[parts.length - 1] ?? source.qualifiedName;
  }
  return source.filePath ?? "核心源码";
}

export function SourceWorkspace({
  sources,
  activeSymbolId,
  title: titleOverride,
  highlightRange,
  onSelectSource,
  onLocateInStructure,
  onClose,
}: SourceWorkspaceProps) {
  const codeContainerRef = useRef<HTMLDivElement | null>(null);
  const usableSources = useMemo(
    () => sources.filter((source) => Boolean(source.symbolId)),
    [sources],
  );
  const activeSource =
    usableSources.find((source) => source.symbolId === activeSymbolId) ??
    usableSources[0] ??
    null;
  const symbolId = activeSource?.symbolId;
  const [snippet, setSnippet] = useState<SourceSnippet | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!symbolId) {
      setSnippet(null);
      setLoading(false);
      setError("该源码引用没有 symbolId，无法定位源码。");
      return;
    }
    const controller = new AbortController();
    setSnippet(null);
    setLoading(true);
    setError(null);
    setCopied(false);

    void fetchSourceSnippet(symbolId, controller.signal)
      .then((nextSnippet) => {
        if (!controller.signal.aborted) {
          setSnippet(nextSnippet);
        }
      })
      .catch((fetchError: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setError(architectureErrorMessage(fetchError));
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });

    return () => {
      controller.abort();
    };
  }, [symbolId]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing) {
        return;
      }
      const target = event.target as HTMLElement | null;
      if (
        target &&
        (target.tagName === "INPUT" ||
          target.tagName === "TEXTAREA" ||
          target.tagName === "SELECT" ||
          target.isContentEditable)
      ) {
        return;
      }
      onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [onClose]);

  useEffect(() => {
    if (!highlightRange || !snippet || !codeContainerRef.current) {
      return;
    }
    const targetEl = codeContainerRef.current.querySelector<HTMLElement>(
      `[data-line-number="${highlightRange.start}"]`,
    );
    targetEl?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [highlightRange, snippet]);

  const title =
    titleOverride ??
    snippet?.symbolName ??
    (activeSource ? sourceLabel(activeSource) : "核心源码");
  const filePath = snippet?.filePath ?? activeSource?.filePath ?? "";
  const symbolStart = snippet?.symbolStartLine ?? activeSource?.startLine;
  const symbolEnd = snippet?.symbolEndLine ?? activeSource?.endLine;
  const symbolRange = formatRange(symbolStart, symbolEnd);
  const returnedStart = snippet?.startLine;
  const returnedEnd = snippet?.endLine;
  const activeIndex = activeSource
    ? usableSources.findIndex(
        (source) => source.symbolId === activeSource.symbolId,
      )
    : -1;

  const handleCopy = () => {
    if (!filePath || !navigator.clipboard) {
      return;
    }
    const pathWithRange = `${filePath}${symbolRange ? `:${symbolRange}` : ""}`;
    void navigator.clipboard
      .writeText(pathWithRange)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1800);
      })
      .catch(() => {
        // 复制不可用时保持按钮静默失败，不影响源码阅读。
      });
  };

  return (
    <section
      className="source-drawer source-workspace"
      role="region"
      aria-label="源码工作台"
      data-testid="source-workspace"
    >
      <header className="source-drawer__header">
        <div className="source-drawer__heading">
          <div className="eyebrow">
            SOURCE WORKSPACE
            {usableSources.length > 1
              ? ` · ${usableSources.length} SOURCES`
              : ""}
          </div>
          <div className="source-drawer__title-row">
            <h3 className="source-drawer__title">{title}</h3>
            {activeSource?.verified ? (
              <span className="source-drawer__verified">已验证</span>
            ) : null}
            {snippet?.kind ? (
              <span className="source-drawer__kind">{snippet.kind}</span>
            ) : null}
          </div>
          <div className="source-drawer__path">
            {filePath || "未知源码路径"}
            {symbolRange ? `:${symbolRange}` : ""}
          </div>
        </div>
        <div className="source-drawer__actions">
          {symbolId && onLocateInStructure ? (
            <button
              type="button"
              className="source-drawer__button"
              onClick={() => onLocateInStructure(symbolId)}
              title="在代码结构图中定位该符号"
            >
              在结构图定位
            </button>
          ) : null}
          {filePath ? (
            <button
              type="button"
              className="source-drawer__button"
              onClick={handleCopy}
              title="复制源码路径与行号"
            >
              {copied ? "已复制" : "复制路径"}
            </button>
          ) : null}
          <button
            type="button"
            className="source-drawer__close"
            onClick={onClose}
            aria-label="关闭源码工作台"
            title="关闭源码工作台 (Esc)"
          >
            ×
          </button>
        </div>
      </header>

      {usableSources.length > 1 ? (
        <div
          className="source-workspace__tabs"
          role="tablist"
          aria-label="节点包含的核心源码"
        >
          {usableSources.map((source) => {
            const active = source.symbolId === activeSource?.symbolId;
            return (
              <button
                key={source.symbolId ?? source.filePath}
                type="button"
                role="tab"
                aria-selected={active}
                className={`source-workspace__tab${
                  active ? " is-active" : ""
                }`}
                onClick={() => onSelectSource(source)}
                title={source.filePath ?? source.qualifiedName}
              >
                <span className="source-workspace__tab-name">
                  {sourceLabel(source)}
                </span>
                <span className="source-workspace__tab-path">
                  {source.filePath ?? "未知路径"}
                </span>
              </button>
            );
          })}
        </div>
      ) : null}

      <div className="source-drawer__meta">
        {snippet?.language ? <span>{snippet.language}</span> : null}
        {symbolRange ? <span>符号 {symbolRange} 行</span> : null}
        {snippet ? (
          <span>
            返回 {returnedStart}-{returnedEnd} / 文件共 {snippet.totalLines} 行
          </span>
        ) : null}
        {usableSources.length > 1 && activeIndex >= 0 ? (
          <span>
            第 {activeIndex + 1}/{usableSources.length} 个符号
          </span>
        ) : null}
        {snippet?.truncated ? (
          <span className="source-drawer__meta-warning">
            源码超过单次展示上限，已截断
          </span>
        ) : null}
      </div>

      {loading ? (
        <div
          className="source-drawer__status source-workspace__status"
          aria-live="polite"
        >
          <span className="source-drawer__spinner" aria-hidden="true" />
          正在读取 {filePath || "源码"}…
        </div>
      ) : error ? (
        <div
          className="source-drawer__status source-drawer__status--error source-workspace__status"
          role="alert"
        >
          <strong>无法读取源码</strong>
          <span>{error}</span>
        </div>
      ) : snippet && snippet.lines.length > 0 ? (
        <div
          ref={codeContainerRef}
          className="source-drawer__code source-workspace__code"
        >
          {snippet.lines.map((line) => {
            const isHighlighted = Boolean(
              highlightRange &&
                line.number >= highlightRange.start &&
                line.number <= highlightRange.end,
            );
            return (
              <div
                key={line.number}
                data-line-number={line.number}
                className={`source-drawer__line${
                  line.number === snippet.symbolStartLine
                    ? " source-drawer__line--symbol-start"
                    : ""
                }${isHighlighted ? " source-drawer__line--highlighted" : ""}`}
              >
                <span className="source-drawer__line-number">{line.number}</span>
                <code>{line.text || " "}</code>
              </div>
            );
          })}
        </div>
      ) : (
        <div className="source-drawer__status source-workspace__status">
          该符号没有可显示的源码正文。
        </div>
      )}

      <footer className="source-workspace__footer">
        <span>只读源码 · 不修改目标项目</span>
        {snippet ? (
          <span>
            {snippet.lines.length} 行 · {filePath}
          </span>
        ) : null}
      </footer>
    </section>
  );
}
