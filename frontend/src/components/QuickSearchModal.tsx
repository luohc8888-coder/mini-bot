import { useEffect, useMemo, useRef, useState } from "react";
import type { GraphNode, NodeKind } from "../types/graph";

const KIND_LABELS: Record<NodeKind, string> = {
  module: "模块",
  class: "类",
  function: "函数",
  method: "方法",
};

interface QuickSearchModalProps {
  open: boolean;
  nodes: GraphNode[];
  onClose: () => void;
  onSelectSymbol: (node: GraphNode, openSource: boolean) => void;
}

export function QuickSearchModal({
  open,
  nodes,
  onClose,
  onSelectSymbol,
}: QuickSearchModalProps) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const searchableNodes = useMemo(
    () => nodes.filter((node) => !node.synthetic),
    [nodes],
  );

  const results = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    if (!keyword) {
      return searchableNodes.slice(0, 16);
    }
    const scored: { node: GraphNode; score: number }[] = [];
    for (const node of searchableNodes) {
      const label = node.label.toLowerCase();
      const qualified = (node.qualifiedName ?? "").toLowerCase();
      const filePath = (node.filePath ?? "").toLowerCase();
      let score = 0;
      if (label === keyword) {
        score = 100;
      } else if (label.startsWith(keyword)) {
        score = 80;
      } else if (label.includes(keyword)) {
        score = 60;
      } else if (qualified.includes(keyword)) {
        score = 45;
      } else if (filePath.includes(keyword)) {
        score = 30;
      }
      if (score > 0) {
        scored.push({ node, score });
      }
    }
    scored.sort(
      (a, b) => b.score - a.score || a.node.label.localeCompare(b.node.label),
    );
    return scored.slice(0, 24).map((item) => item.node);
  }, [query, searchableNodes]);

  useEffect(() => {
    if (!open) {
      return;
    }
    setQuery("");
    setActiveIndex(0);
    const timer = window.setTimeout(() => {
      inputRef.current?.focus();
    }, 10);
    return () => window.clearTimeout(timer);
  }, [open]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  if (!open) {
    return null;
  }

  const clampedIndex = Math.min(activeIndex, Math.max(0, results.length - 1));

  return (
    <div
      className="quick-search-backdrop"
      role="presentation"
      onClick={onClose}
    >
      <div
        className="quick-search-modal"
        role="dialog"
        aria-modal="true"
        aria-label="全局符号搜索"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="quick-search-modal__input-row">
          <span className="quick-search-modal__icon" aria-hidden="true">
            ⌕
          </span>
          <input
            ref={inputRef}
            type="text"
            className="quick-search-modal__input"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.nativeEvent.isComposing) {
                return;
              }
              if (event.key === "Escape") {
                event.preventDefault();
                onClose();
              } else if (event.key === "ArrowDown") {
                event.preventDefault();
                setActiveIndex((prev) =>
                  results.length > 0 ? (prev + 1) % results.length : 0,
                );
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                setActiveIndex((prev) =>
                  results.length > 0
                    ? (prev - 1 + results.length) % results.length
                    : 0,
                );
              } else if (event.key === "Enter" && results[clampedIndex]) {
                event.preventDefault();
                // Enter 默认直接打开源码；Shift/Cmd/Ctrl + Enter 在代码结构图中定位
                onSelectSymbol(
                  results[clampedIndex],
                  !(event.shiftKey || event.metaKey || event.ctrlKey),
                );
              }
            }}
            placeholder="搜索模块、类、函数、方法或文件路径…"
            aria-label="搜索符号"
          />
          <kbd className="quick-search-modal__kbd">ESC</kbd>
        </div>

        <div className="quick-search-modal__list" role="listbox">
          {results.length === 0 ? (
            <div className="quick-search-modal__empty">
              未找到匹配“{query}”的代码符号
            </div>
          ) : (
            results.map((node, index) => {
              const active = index === clampedIndex;
              return (
                <div
                  key={node.id}
                  role="option"
                  aria-selected={active}
                  className={`quick-search-modal__item${active ? " is-active" : ""}`}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => onSelectSymbol(node, true)}
                >
                  <span className={`quick-search-modal__kind kind-${node.kind}`}>
                    {KIND_LABELS[node.kind]}
                  </span>
                  <span className="quick-search-modal__copy">
                    <strong>{node.label}</strong>
                    <span>
                      {node.filePath ?? node.qualifiedName ?? node.id}
                      {node.startLine ? `:${node.startLine}` : ""}
                    </span>
                  </span>
                  <span className="quick-search-modal__actions">
                    <button
                      type="button"
                      className="quick-search-modal__action"
                      onClick={(event) => {
                        event.stopPropagation();
                        onSelectSymbol(node, false);
                      }}
                      title="在代码结构图中定位该符号"
                    >
                      定位
                    </button>
                    <button
                      type="button"
                      className="quick-search-modal__action quick-search-modal__action--primary"
                      onClick={(event) => {
                        event.stopPropagation();
                        onSelectSymbol(node, true);
                      }}
                      title="打开源码工作台与 AI 对话"
                    >
                      打开源码
                    </button>
                  </span>
                </div>
              );
            })
          )}
        </div>

        <footer className="quick-search-modal__footer">
          <span>↑↓ 选择 · 点击行或 Enter 打开源码</span>
          <span>Shift + Enter 在代码结构中定位</span>
        </footer>
      </div>
    </div>
  );
}
