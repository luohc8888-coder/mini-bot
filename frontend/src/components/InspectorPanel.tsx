import { useEffect, useState } from "react";
import {
  architectureErrorMessage,
  fetchSourceSnippet,
} from "../architecture/api";
import type { NodeRelationItem } from "../graph/data";
import type { SourceSnippet } from "../types/architecture";
import type { MockNode, MockProject, NodeKind, ViewStats } from "../types/graph";

const KIND_LABELS: Record<NodeKind, string> = {
  module: "模块",
  class: "类",
  function: "函数",
  method: "方法",
};

const KIND_ICONS: Record<NodeKind, string> = {
  module: "▣",
  class: "◆",
  function: "ƒ",
  method: "↳",
};

interface InspectorPanelProps {
  node: MockNode | null;
  selected: boolean;
  project: MockProject;
  stats: ViewStats;
  childSummary: string;
  hasChildren: boolean;
  incoming: NodeRelationItem[];
  outgoing: NodeRelationItem[];
  aiConfigured: boolean;
  onClearSelection: () => void;
  onDrillDown: (nodeId: string) => void;
  onOpenSource: (node: MockNode) => void;
  onNavigateNode: (nodeId: string) => void;
}

function kindClass(node: MockNode | null): string {
  return node ? `kind-${node.kind}` : "kind-project";
}

export function InspectorPanel({
  node,
  selected,
  project,
  stats,
  childSummary,
  hasChildren,
  incoming,
  outgoing,
  aiConfigured,
  onClearSelection,
  onDrillDown,
  onOpenSource,
  onNavigateNode,
}: InspectorPanelProps) {
  const canOpenSource = Boolean(node && !node.synthetic);
  const symbolId = canOpenSource ? node?.id ?? null : null;
  const [preview, setPreview] = useState<SourceSnippet | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);

  useEffect(() => {
    if (!symbolId) {
      setPreview(null);
      setPreviewLoading(false);
      setPreviewError(null);
      return;
    }
    const controller = new AbortController();
    setPreviewLoading(true);
    setPreviewError(null);
    void fetchSourceSnippet(symbolId, controller.signal, 36)
      .then((snippet) => {
        if (!controller.signal.aborted) {
          setPreview(snippet);
        }
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted) {
          setPreview(null);
          setPreviewError(architectureErrorMessage(error));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setPreviewLoading(false);
        }
      });
    return () => controller.abort();
  }, [symbolId]);

  return (
    <aside className="inspector">
      <div className="inspector__header">
        <div>
          <div className="eyebrow">NODE INSPECTOR</div>
          <h2 className="inspector__title">
            {node ? node.label : project.name}
          </h2>
        </div>
        {selected ? (
          <button
            type="button"
            className="ghost-button"
            onClick={onClearSelection}
            title="清除选中"
          >
            清除
          </button>
        ) : null}
      </div>

      {node ? (
        <>
          <div className="inspector__badges">
            <span className={`kind-pill ${kindClass(node)}`}>
              <span aria-hidden="true">{KIND_ICONS[node.kind]}</span>
              {node.synthetic ? "包目录" : KIND_LABELS[node.kind]}
            </span>
            {selected ? <span className="soft-pill">已选中</span> : null}
          </div>

          <p className="inspector__summary">
            {node.summary ?? "该符号暂无文档字符串。"}
          </p>

          <div className="inspector__cta-row">
            {hasChildren ? (
              <button
                type="button"
                className="inspector__cta-btn"
                onClick={() => onDrillDown(node.id)}
              >
                进入内部结构 →
              </button>
            ) : null}
            {canOpenSource ? (
              <button
                type="button"
                className="inspector__cta-btn inspector__cta-btn--primary"
                onClick={() => onOpenSource(node)}
              >
                {aiConfigured ? "打开源码与 AI 对话" : "打开源码工作台"}
              </button>
            ) : null}
          </div>

          <div className="meta-grid">
            <div className="meta-grid__item">
              <span>路径</span>
              <strong title={node.filePath ?? node.path ?? ""}>
                {node.filePath ?? node.path ?? "—"}
              </strong>
            </div>
            <div className="meta-grid__item">
              <span>范围</span>
              <strong>
                {node.startLine
                  ? `${node.startLine} – ${node.endLine ?? node.startLine} 行`
                  : "目录容器"}
              </strong>
            </div>
            {node.signature ? (
              <div className="meta-grid__item meta-grid__item--wide">
                <span>签名</span>
                <strong className="mono">{node.signature}</strong>
              </div>
            ) : null}
            {childSummary ? (
              <div className="meta-grid__item meta-grid__item--wide">
                <span>包含</span>
                <strong>{childSummary}</strong>
              </div>
            ) : null}
          </div>
        </>
      ) : (
        <>
          <div className="inspector__badges">
            <span className="kind-pill kind-project">项目</span>
            <span className="soft-pill">实时 AST 索引</span>
          </div>
          <p className="inspector__summary">{project.description}</p>
          <div className="project-stats">
            <div>
              <strong>{stats.modules}</strong>
              <span>模块</span>
            </div>
            <div>
              <strong>{stats.classes}</strong>
              <span>类</span>
            </div>
            <div>
              <strong>{stats.functions}</strong>
              <span>函数</span>
            </div>
            <div>
              <strong>{stats.methods}</strong>
              <span>方法</span>
            </div>
          </div>
          <div className="empty-hint">
            <span className="empty-hint__icon">⌁</span>
            <div>
              <strong>单击节点看详情，双击下钻或打开源码</strong>
              <p>
                沿项目 → 目录 → 模块 → 类 → 方法逐层探索；按 ⌘K / Ctrl+K
                可全局搜索任意符号。
              </p>
            </div>
          </div>
        </>
      )}

      {node && (incoming.length > 0 || outgoing.length > 0) ? (
        <section className="panel-section">
          <div className="panel-section__title">
            <span>关联调用与依赖</span>
            <span className="section-tag">
              入 {incoming.length} · 出 {outgoing.length}
            </span>
          </div>
          {incoming.length > 0 ? (
            <div className="relation-group">
              <div className="relation-group__label">
                被以下节点调用 / 引用 ({incoming.length})
              </div>
              <div className="relation-chip-list">
                {incoming.slice(0, 12).map(({ node: related, edgeKind }) => (
                  <button
                    key={`in-${edgeKind}-${related.id}`}
                    type="button"
                    className="relation-chip"
                    onClick={() => onNavigateNode(related.id)}
                    title={related.qualifiedName ?? related.filePath ?? related.label}
                  >
                    <span className={`relation-chip__dot kind-${related.kind}`} />
                    <span className="relation-chip__name">{related.label}</span>
                    <span className="relation-chip__kind">
                      {edgeKind === "calls" ? "调用" : "导入"}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          ) : null}
          {outgoing.length > 0 ? (
            <div className="relation-group">
              <div className="relation-group__label">
                调用 / 依赖以下节点 ({outgoing.length})
              </div>
              <div className="relation-chip-list">
                {outgoing.slice(0, 12).map(({ node: related, edgeKind }) => (
                  <button
                    key={`out-${edgeKind}-${related.id}`}
                    type="button"
                    className="relation-chip"
                    onClick={() => onNavigateNode(related.id)}
                    title={related.qualifiedName ?? related.filePath ?? related.label}
                  >
                    <span className={`relation-chip__dot kind-${related.kind}`} />
                    <span className="relation-chip__name">{related.label}</span>
                    <span className="relation-chip__kind">
                      {edgeKind === "calls" ? "调用" : "导入"}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          ) : null}
        </section>
      ) : null}

      <section className="panel-section">
        <div className="panel-section__title">
          <span>源码预览</span>
          {canOpenSource ? (
            <button
              type="button"
              className="source-ref__open"
              onClick={() => node && onOpenSource(node)}
            >
              完整源码 / AI 对话
            </button>
          ) : (
            <span className="section-tag">只读</span>
          )}
        </div>
        {canOpenSource ? (
          previewLoading ? (
            <div className="panel-empty">
              <span className="source-drawer__spinner" aria-hidden="true" />
              <p>正在读取 {node?.filePath ?? "源码"}…</p>
            </div>
          ) : previewError ? (
            <div className="panel-empty">
              <span className="panel-empty__icon">!</span>
              <p>{previewError}</p>
            </div>
          ) : preview && preview.lines.length > 0 ? (
            <div className="code-block" aria-label="源码片段预览">
              {preview.lines.map((line) => (
                <div className="code-line" key={line.number}>
                  <span className="code-line__number">{line.number}</span>
                  <code>{line.text || " "}</code>
                </div>
              ))}
              {preview.truncated ? (
                <div className="code-block__more">
                  <button
                    type="button"
                    className="source-ref__open"
                    onClick={() => node && onOpenSource(node)}
                  >
                    展开完整符号源码（共 {preview.symbolEndLine - preview.symbolStartLine + 1} 行）→
                  </button>
                </div>
              ) : null}
            </div>
          ) : (
            <div className="panel-empty">
              <span className="panel-empty__icon">⌘</span>
              <p>该符号没有可预览的源码正文。</p>
            </div>
          )
        ) : (
          <div className="panel-empty">
            <span className="panel-empty__icon">⌘</span>
            <p>目录节点不直接对应单个文件。请双击进入并选择模块、类或函数。</p>
          </div>
        )}
      </section>

      <div className="inspector__footer">
        <span className="status-dot" />
        <span>只读 · 不修改目标项目</span>
      </div>
    </aside>
  );
}
