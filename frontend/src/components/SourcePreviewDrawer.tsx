import { useEffect, useState } from "react";
import {
  architectureErrorMessage,
  fetchSourceSnippet,
} from "../architecture/api";
import type {
  ArchitectureSourceRef,
  SourceSnippet,
} from "../types/architecture";

interface SourcePreviewDrawerProps {
  source: ArchitectureSourceRef;
  onClose: () => void;
}

function formatRange(start?: number, end?: number): string {
  if (!start) {
    return "";
  }
  return end && end !== start ? `${start}-${end}` : String(start);
}

export function SourcePreviewDrawer({
  source,
  onClose,
}: SourcePreviewDrawerProps) {
  const symbolId = source.symbolId;
  const [snippet, setSnippet] = useState<SourceSnippet | null>(null);
  const [loading, setLoading] = useState(Boolean(symbolId));
  const [error, setError] = useState<string | null>(
    symbolId ? null : "该源码引用没有 symbolId，无法定位源码。",
  );
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!symbolId) {
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
      if (event.key === "Escape") {
        onClose();
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [onClose]);

  const title =
    snippet?.symbolName ??
    source.symbolName ??
    source.qualifiedName ??
    source.symbolId ??
    "源码引用";
  const filePath = snippet?.filePath ?? source.filePath ?? "";
  const symbolStart = snippet?.symbolStartLine ?? source.startLine;
  const symbolEnd = snippet?.symbolEndLine ?? source.endLine;
  const symbolRange = formatRange(symbolStart, symbolEnd);
  const returnedStart = snippet?.startLine;
  const returnedEnd = snippet?.endLine;

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
      className="source-drawer"
      role="region"
      aria-label="源码预览"
      data-testid="source-preview-drawer"
    >
      <header className="source-drawer__header">
        <div className="source-drawer__heading">
          <div className="eyebrow">SOURCE TRACE</div>
          <div className="source-drawer__title-row">
            <h3 className="source-drawer__title">{title}</h3>
            {source.verified ? (
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
            aria-label="关闭源码预览"
            title="关闭源码预览"
          >
            ×
          </button>
        </div>
      </header>

      <div className="source-drawer__meta">
        <span>{snippet?.language ?? "python"}</span>
        {snippet ? (
          <span>
            符号范围 {formatRange(snippet.symbolStartLine, snippet.symbolEndLine)}
          </span>
        ) : null}
        {snippet?.truncated ? (
          <span className="source-drawer__meta-warning">
            已截断，仅显示 {returnedStart}-{returnedEnd} 行
          </span>
        ) : null}
        {snippet && !snippet.truncated ? (
          <span>共 {snippet.lines.length} 行</span>
        ) : null}
      </div>

      {loading ? (
        <div className="source-drawer__status" aria-live="polite">
          <span className="source-drawer__spinner" aria-hidden="true" />
          正在读取源码…
        </div>
      ) : error ? (
        <div className="source-drawer__status source-drawer__status--error" role="alert">
          <strong>源码读取失败</strong>
          <span>{error}</span>
        </div>
      ) : snippet ? (
        <div className="source-drawer__code" role="presentation">
          {snippet.lines.length > 0 ? (
            snippet.lines.map((line) => (
              <div
                className={`source-drawer__line${
                  line.number === snippet.symbolStartLine
                    ? " source-drawer__line--symbol-start"
                    : ""
                }`}
                key={line.number}
              >
                <span className="source-drawer__line-number">
                  {line.number}
                </span>
                <code>{line.text || " "}</code>
              </div>
            ))
          ) : (
            <div className="source-drawer__status">该符号没有可显示的源码正文。</div>
          )}
        </div>
      ) : null}
    </section>
  );
}
