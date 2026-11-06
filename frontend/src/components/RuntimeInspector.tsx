import type {
  ArchitectureGraph,
  ArchitectureNode,
  ArchitectureNodeKind,
  ArchitectureView,
} from "../types/architecture";

const KIND_LABELS: Record<ArchitectureNodeKind, string> = {
  component: "逻辑组件",
  stage: "执行阶段",
  condition: "判断条件",
};

const KIND_ICONS: Record<ArchitectureNodeKind, string> = {
  component: "◆",
  stage: "▸",
  condition: "◇",
};

export interface RuntimeAnalysisMeta {
  provider: string;
  model: string;
  source: "cache" | "generated" | null;
}

interface RuntimeInspectorProps {
  node: ArchitectureNode | null;
  view: ArchitectureView;
  graph: ArchitectureGraph;
  selected: boolean;
  onClearSelection: () => void;
  analysis?: RuntimeAnalysisMeta | null;
}

export function RuntimeInspector({
  node,
  view,
  graph,
  selected,
  onClearSelection,
  analysis,
}: RuntimeInspectorProps) {
  const isMock = graph.mock;
  const sourceRefs = node?.sourceRefs ?? [];
  const verifiedCount = sourceRefs.filter((source) => source.verified).length;
  const componentCount = view.nodes.filter(
    (item) => item.kind === "component",
  ).length;
  const conditionCount = view.nodes.filter(
    (item) => item.kind === "condition",
  ).length;

  return (
    <aside className="inspector runtime-inspector">
      <div className="inspector__header">
        <div>
          <div className="eyebrow">ARCHITECTURE INSPECTOR</div>
          <h2 className="inspector__title">
            {node ? node.label : graph.project.name}
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
            <span className={`kind-pill kind-runtime-${node.kind}`}>
              <span aria-hidden="true">{KIND_ICONS[node.kind]}</span>
              {KIND_LABELS[node.kind]}
            </span>
            <span className={`soft-pill${isMock ? "" : " soft-pill--verified"}`}>
              {isMock ? "Mock 组件" : "AI 组件"}
            </span>
            {selected ? <span className="soft-pill">已选中</span> : null}
          </div>

          <p className="inspector__summary">{node.description}</p>

          <div className="meta-grid">
            <div className="meta-grid__item">
              <span>所在视图</span>
              <strong>{view.title}</strong>
            </div>
            <div className="meta-grid__item">
              <span>节点类型</span>
              <strong>{KIND_LABELS[node.kind]}</strong>
            </div>
            <div className="meta-grid__item meta-grid__item--wide">
              <span>源码关联</span>
              <strong>
                {sourceRefs.length > 0
                  ? `${sourceRefs.length} 个符号引用${
                      isMock ? "" : ` · ${verifiedCount} 已验证`
                    }`
                  : isMock
                    ? "尚未关联源码，等待 Phase 4.3 校验"
                    : "AI 未为该节点关联源码"}
              </strong>
            </div>
          </div>
        </>
      ) : (
        <>
          <div className="inspector__badges">
            <span className="kind-pill kind-runtime-component">项目架构</span>
            <span className={`soft-pill${isMock ? "" : " soft-pill--verified"}`}>
              {isMock ? "Phase 4.1 Mock" : "Phase 4.3 LLM"}
            </span>
          </div>
          <p className="inspector__summary">{graph.project.description}</p>
          <div className="project-stats">
            <div>
              <strong>{view.nodes.length}</strong>
              <span>节点</span>
            </div>
            <div>
              <strong>{view.edges.length}</strong>
              <span>连线</span>
            </div>
            <div>
              <strong>{componentCount}</strong>
              <span>组件</span>
            </div>
            <div>
              <strong>{conditionCount}</strong>
              <span>条件</span>
            </div>
          </div>
          <div className="empty-hint">
            <span className="empty-hint__icon">⌘</span>
            <div>
              <strong>单击逻辑组件选中，双击进入内部流程</strong>
              <p>
                带下钻入口的组件可双击查看内部执行流程；带源码引用的组件可在下方查看
                SymbolIndex 校验结果。
              </p>
            </div>
          </div>
        </>
      )}

      {node ? (
        <section className="panel-section">
          <div className="panel-section__title">
            <span>组件职责</span>
            <span className="section-tag">{isMock ? "Mock" : "LLM"}</span>
          </div>
          <ul className="responsibility-list">
            {(node.responsibilities ?? ["当前节点暂无职责描述。"]).map(
              (responsibility) => (
                <li key={responsibility}>{responsibility}</li>
              ),
            )}
          </ul>
        </section>
      ) : null}

      {node?.nested?.length ? (
        <section className="panel-section">
          <div className="panel-section__title">
            <span>嵌套组件</span>
            <span className="section-tag section-tag--warm">视觉嵌套</span>
          </div>
          <div className="nested-reference-list">
            {node.nested.map((nested) => (
              <div className="nested-reference" key={nested.id}>
                <span className="nested-reference__icon" aria-hidden="true">
                  {KIND_ICONS[nested.kind]}
                </span>
                <div>
                  <strong>{nested.label}</strong>
                  <span>点击架构图中的嵌套卡片进入内部流程</span>
                </div>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      <section className="panel-section">
        <div className="panel-section__title">
          <span>源码关联</span>
          <span className="section-tag section-tag--warm">
            {isMock
              ? "Mock / 待校验"
              : `${verifiedCount}/${sourceRefs.length} 已验证`}
          </span>
        </div>
        {sourceRefs.length > 0 ? (
          <div className="source-ref-list">
            {sourceRefs.map((source, index) => (
              <div
                className="source-ref"
                key={`${source.symbolId ?? source.filePath ?? "ref"}:${index}`}
              >
                <div className="source-ref__symbol">
                  <span aria-hidden="true">↳</span>
                  {source.symbolName ??
                    source.qualifiedName ??
                    source.symbolId ??
                    "关联符号"}
                  {source.verified ? (
                    <span className="source-ref__verified">已验证</span>
                  ) : null}
                </div>
                <div className="source-ref__path">
                  {source.filePath ?? source.qualifiedName ?? "未知源码路径"}
                  {source.startLine
                    ? `:${source.startLine}-${source.endLine ?? source.startLine}`
                    : ""}
                </div>
                {source.symbolId ? (
                  <div className="source-ref__id">{source.symbolId}</div>
                ) : null}
              </div>
            ))}
          </div>
        ) : (
          <div className="panel-empty">
            <span className="panel-empty__icon">⌘</span>
            <p>
              {isMock
                ? "该逻辑节点当前没有源码引用。Phase 4.3 会通过 SymbolIndex 校验引用，无法静态确认的关系将标记为 inferred。"
                : "该节点当前没有关联源码引用；架构关系仍可在上方画布中查看。"}
            </p>
          </div>
        )}
      </section>

      <section className="panel-section">
        <div className="panel-section__title">
          <span>AI 语义分析</span>
          <span className="section-tag section-tag--warm">
            {isMock ? "Phase 4.3" : "LLM 已生成"}
          </span>
        </div>
        {isMock ? (
          <div className="ai-reserved">
            <div className="ai-reserved__intro">
              <span className="ai-reserved__spark">✦</span>
              <p>
                当前是 Phase 4.1 的 Mock ArchitectureGraph，不会调用 LLM。
                Phase 4.3 运行架构由结构化索引与按需源码窗口生成，并经过
                SymbolIndex 校验。
              </p>
            </div>
          </div>
        ) : (
          <div className="ai-reserved ai-reserved--live">
            <div className="ai-reserved__intro">
              <span className="ai-reserved__spark">✦</span>
              <p>
                本图由 LLM 基于 AST/CodeGraph 结构化输入生成。源码引用已通过
                SymbolIndex 校验；无法静态确认的关系标记为 inferred。
              </p>
            </div>
            <div className="prompt-chips">
              <span>{analysis?.provider || "LLM Provider"}</span>
              <span>{analysis?.model || "当前模型"}</span>
              <span>
                {analysis?.source === "cache" ? "缓存结果" : "本次生成"}
              </span>
            </div>
          </div>
        )}
      </section>

      <div className="inspector__footer">
        <span
          className={
            isMock ? "status-dot status-dot--mock" : "status-dot status-dot--green"
          }
        />
        <span>
          {isMock
            ? "Mock 架构图 · 只读原型 · 不修改目标项目"
            : "AI 架构图 · SymbolIndex 校验 · 只读分析"}
        </span>
      </div>
    </aside>
  );
}
