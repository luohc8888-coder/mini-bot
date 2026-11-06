import { useMemo, useState } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { Breadcrumb } from "../components/Breadcrumb";
import { GraphCanvas } from "../components/GraphCanvas";
import { InspectorPanel } from "../components/InspectorPanel";
import {
  buildViewModel,
  getNode,
  getProjectStats,
  hasChildren,
  mockGraph,
} from "../graph/mockGraph";

const KIND_LABELS = {
  module: "模块",
  class: "类",
  function: "函数",
  method: "方法",
} as const;

export function ExplorerPage() {
  const [containerId, setContainerId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const stats = getProjectStats();

  const view = useMemo(
    () => buildViewModel(containerId, selectedId),
    [containerId, selectedId],
  );

  const handleNodeClick = (nodeId: string) => {
    const node = getNode(nodeId);
    if (!node) {
      return;
    }
    if (hasChildren(nodeId)) {
      setContainerId(nodeId);
      setSelectedId(null);
      return;
    }
    setSelectedId(nodeId);
  };

  const handleBack = () => {
    if (!containerId) {
      return;
    }
    const current = getNode(containerId);
    setContainerId(current?.parentId ?? null);
    setSelectedId(null);
  };

  const handleBreadcrumb = (nodeId: string | null) => {
    setContainerId(nodeId);
    setSelectedId(null);
  };

  const containerNode = getNode(containerId);
  const currentLabel = containerNode?.label ?? mockGraph.project.name;
  const currentNodeKind = containerNode
    ? KIND_LABELS[containerNode.kind]
    : "项目";

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand">
          <div className="brand__mark" aria-hidden="true">
            <span>MH</span>
          </div>
          <div className="brand__copy">
            <div className="brand__title">
              Mini Harness <span>/ Code Explorer</span>
            </div>
            <div className="brand__subtitle">
              本地代码理解工作台 · Phase 1 UI Prototype
            </div>
          </div>
        </div>

        <div className="header-meta">
          <span className="workspace-chip">
            <span className="status-dot" />
            {mockGraph.project.root}
          </span>
          <span className="mock-badge">MOCK JSON</span>
          <span className="version-chip">V3.0</span>
        </div>
      </header>

      <main className="workspace">
        <section className="graph-column">
          <div className="graph-toolbar">
            <button
              type="button"
              className="back-button"
              onClick={handleBack}
              disabled={!containerId}
              title="返回上一级"
            >
              <span aria-hidden="true">←</span>
              返回
            </button>

            <div className="toolbar-divider" aria-hidden="true" />

            <Breadcrumb
              items={view.breadcrumbs}
              onNavigate={handleBreadcrumb}
            />

            <div className="toolbar-spacer" />

            <div className="view-context">
              <span className={`context-dot kind-${containerNode?.kind ?? "project"}`} />
              <span>{currentLabel}</span>
              <span className="view-context__kind">{currentNodeKind}</span>
            </div>
            <span className="view-stats">
              {view.visibleNodes.length} 节点 · {view.visibleEdges.length} 关系
            </span>
          </div>

          <div className="graph-stage">
            <div className="graph-hints">
              <span>拖拽节点</span>
              <span className="hint-separator">·</span>
              <span>滚轮缩放</span>
              <span className="hint-separator">·</span>
              <span>点击容器下钻</span>
            </div>

            <ReactFlowProvider>
              <GraphCanvas
                viewKey={containerId ?? "root"}
                nodes={view.visibleNodes}
                edges={view.visibleEdges}
                selectedId={selectedId}
                onNodeClick={handleNodeClick}
                onPaneClick={() => setSelectedId(null)}
              />
            </ReactFlowProvider>

            {view.visibleNodes.length === 0 ? (
              <div className="graph-empty">
                <span className="graph-empty__icon">⌁</span>
                <h3>这一层没有可展示的符号</h3>
                <p>可以使用上方面包屑返回项目总览。</p>
              </div>
            ) : null}

            <div className="graph-legend" aria-label="图例">
              <span className="legend-group">
                <i className="legend-dot legend-dot--module" />
                模块
              </span>
              <span className="legend-group">
                <i className="legend-dot legend-dot--class" />
                类
              </span>
              <span className="legend-group">
                <i className="legend-dot legend-dot--function" />
                函数
              </span>
              <span className="legend-group">
                <i className="legend-dot legend-dot--method" />
                方法
              </span>
              <span className="legend-divider" />
              <span className="legend-group legend-group--muted">
                <i className="legend-line" />
                静态关系
              </span>
              <span className="legend-group legend-group--muted">
                <i className="legend-line legend-line--dash" />
                推断关系
              </span>
            </div>
          </div>
        </section>

        <InspectorPanel
          node={view.inspectorNode}
          selected={selectedId !== null}
          onClearSelection={() => setSelectedId(null)}
        />
      </main>

      <footer className="status-bar">
        <span>
          <span className="status-dot status-dot--green" />
          固定 Workspace：{mockGraph.project.root}
        </span>
        <span className="status-bar__right">
          <span>React Flow · Dagre</span>
          <span className="status-bar__divider">/</span>
          <span>Mock 数据 · 尚未连接 Python 后端</span>
          <span className="status-bar__divider">/</span>
          <span>{stats.modules + stats.classes + stats.functions + stats.methods} 个符号</span>
        </span>
      </footer>
    </div>
  );
}
