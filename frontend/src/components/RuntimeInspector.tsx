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

interface RuntimeInspectorProps {
  node: ArchitectureNode | null;
  view: ArchitectureView;
  graph: ArchitectureGraph;
  selected: boolean;
  onClearSelection: () => void;
}

export function RuntimeInspector({
  node,
  view,
  graph,
  selected,
  onClearSelection,
}: RuntimeInspectorProps) {
  const sourceRefs = node?.sourceRefs ?? [];
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
            <span className="soft-pill">Mock 组件</span>
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
                  ? `${sourceRefs.length} 个符号引用`
                  : "尚未关联源码，等待 Phase 4.3 校验"}
              </strong>
            </div>
          </div>
        </>
      ) : (
        <>
          <div className="inspector__badges">
            <span className="kind-pill kind-runtime-component">项目架构</span>
            <span className="soft-pill">Phase 4.1 Mock</span>
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
              <strong>点击逻辑组件开始下钻</strong>
              <p>
                例如点击 AgentLoop 查看消息循环，或点击其内部的 AgentRunner
                查看工具调用分支。
              </p>
            </div>
          </div>
        </>
      )}

      {node ? (
        <section className="panel-section">
          <div className="panel-section__title">
            <span>组件职责</span>
            <span className="section-tag">Mock</span>
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
          <span className="section-tag section-tag--warm">Mock / 待校验</span>
        </div>
        {sourceRefs.length > 0 ? (
          <div className="source-ref-list">
            {sourceRefs.map((source) => (
              <div className="source-ref" key={`${source.filePath}:${source.startLine}`}>
                <div className="source-ref__symbol">
                  <span aria-hidden="true">↳</span>
                  {source.symbolName ?? "关联符号"}
                </div>
                <div className="source-ref__path">
                  {source.filePath}
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
              该逻辑节点当前没有源码引用。Phase 4.3 会通过 SymbolIndex 校验引用，
              无法静态确认的关系将标记为 inferred。
            </p>
          </div>
        )}
      </section>

      <section className="panel-section">
        <div className="panel-section__title">
          <span>AI 语义分析</span>
          <span className="section-tag section-tag--warm">Phase 4.3</span>
        </div>
        <div className="ai-reserved">
          <div className="ai-reserved__intro">
            <span className="ai-reserved__spark">✦</span>
            <p>
              当前是 Phase 4.1 的 Mock ArchitectureGraph，不会调用 LLM。
              后续阶段将基于结构化索引与按需读取的源码生成可验证的架构图。
            </p>
          </div>
        </div>
      </section>

      <div className="inspector__footer">
        <span className="status-dot" />
        <span>Mock 架构图 · 只读原型 · 不修改目标项目</span>
      </div>
    </aside>
  );
}
