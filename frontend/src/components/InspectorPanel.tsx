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
  onClearSelection: () => void;
}

function codePlaceholder(node: MockNode): string[] {
  const source = node.filePath ?? node.path ?? "unknown";
  const range =
    node.startLine && node.endLine
      ? `第 ${node.startLine}-${node.endLine} 行`
      : "容器节点";
  return [
    `# ${source}`,
    `# ${range}`,
    "",
    "# Phase 5 将在这里加载真实源码",
    "# 并由 Monaco Editor 高亮对应符号范围。",
  ];
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
  onClearSelection,
}: InspectorPanelProps) {
  const isLeaf = node?.kind === "function" || node?.kind === "method";
  const lines = node
    ? node.sourcePreview ?? codePlaceholder(node)
    : codePlaceholder({
        id: "project",
        kind: "module",
        label: project.name,
        parentId: null,
        path: project.root,
      });
  const startLine = node?.startLine ?? 1;

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
              {KIND_LABELS[node.kind]}
            </span>
            {selected ? <span className="soft-pill">已选中</span> : null}
          </div>

          <p className="inspector__summary">
            {node.summary ?? "该节点暂无摘要。"}
          </p>

          <div className="meta-grid">
            <div className="meta-grid__item">
              <span>路径</span>
              <strong>{node.filePath ?? node.path ?? "—"}</strong>
            </div>
            <div className="meta-grid__item">
              <span>范围</span>
              <strong>
                {node.startLine
                  ? `${node.startLine} – ${node.endLine ?? node.startLine} 行`
                  : "容器节点"}
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
            <span className="soft-pill">实时索引</span>
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
              <strong>点击模块开始浏览</strong>
              <p>沿项目 → 模块 → 类 → 方法逐层下钻；面包屑可随时返回。</p>
            </div>
          </div>
        </>
      )}

      <section className="panel-section">
        <div className="panel-section__title">
          <span>源码定位</span>
          <span className="section-tag">Phase 5</span>
        </div>
        {isLeaf ? (
          <div className="code-block" aria-label="源码占位区域">
            {lines.map((line, index) => (
              <div className="code-line" key={`${index}-${line}`}>
                <span className="code-line__number">{startLine + index}</span>
                <code>{line}</code>
              </div>
            ))}
          </div>
        ) : (
          <div className="panel-empty">
            <span className="panel-empty__icon">⌘</span>
            <p>容器节点不直接展示源码。请选择一个函数或方法。</p>
          </div>
        )}
      </section>

      <section className="panel-section">
        <div className="panel-section__title">
          <span>AI 代码解释</span>
          <span className="section-tag section-tag--warm">Phase 6</span>
        </div>
        <div className="ai-reserved">
          <div className="ai-reserved__intro">
            <span className="ai-reserved__spark">✦</span>
            <p>
              选中代码后，AI 将基于所属方法、直接调用链和必要代码片段进行解释，
              不会把整个项目发送给模型。
            </p>
          </div>
          <div className="prompt-chips" aria-hidden="true">
            <span>解释作用</span>
            <span>分析调用链</span>
            <span>潜在问题</span>
          </div>
          <textarea
            className="ai-reserved__input"
            placeholder="补充问题，例如：这个方法为什么需要 ContextGovernor？"
            disabled
            rows={2}
          />
          <button type="button" className="primary-button" disabled>
            接入 LLMProvider 后可提问
          </button>
          <p className="privacy-note">
            <span aria-hidden="true">◇</span>
            远端模型调用前会明确提示：源码数据可能离开本机。
          </p>
        </div>
      </section>

      <div className="inspector__footer">
        <span className="status-dot" />
        <span>只读 · 不修改目标项目</span>
      </div>
    </aside>
  );
}
