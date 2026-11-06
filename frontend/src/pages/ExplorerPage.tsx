import { useEffect, useMemo, useState } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { Breadcrumb } from "../components/Breadcrumb";
import { GraphCanvas } from "../components/GraphCanvas";
import { InspectorPanel } from "../components/InspectorPanel";
import { fetchExplorerData } from "../graph/api";
import {
  buildGraphData,
  buildGraphIndex,
  buildViewModel,
  getChildSummary,
  getChildren,
  getNode,
  type GraphData,
  type GraphIndex,
} from "../graph/data";
import type { NodeKind } from "../types/graph";

const KIND_LABELS: Record<NodeKind, string> = {
  module: "模块",
  class: "类",
  function: "函数",
  method: "方法",
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function ExplorerPage() {
  const [data, setData] = useState<GraphData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [containerId, setContainerId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetchExplorerData()
      .then((payload) => {
        if (cancelled) {
          return;
        }
        setData(buildGraphData(payload.project, payload.graph));
      })
      .catch((reason: unknown) => {
        if (!cancelled) {
          setError(errorMessage(reason));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const index = useMemo<GraphIndex | null>(
    () => (data ? buildGraphIndex(data) : null),
    [data],
  );

  const view = useMemo(
    () =>
      index
        ? buildViewModel(index, containerId, selectedId)
        : null,
    [index, containerId, selectedId],
  );

  const header = (
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
            本地代码理解工作台 · Phase 4 Live
          </div>
        </div>
      </div>

      <div className="header-meta">
        <span className="workspace-chip">
          <span className="status-dot" />
          {data?.project.root ?? "正在连接本地服务..."}
        </span>
        <span className="live-badge">LIVE API</span>
        <span className="version-chip">V3.0</span>
      </div>
    </header>
  );

  if (!data || !index || !view) {
    return (
      <div className="app-shell">
        {header}
        <main className="state-screen">
          {loading ? (
            <div className="state-card">
              <span className="state-spinner" aria-hidden="true" />
              <div className="eyebrow">CONNECTING</div>
              <h2>正在读取当前 Workspace 的 CodeGraph…</h2>
              <p>首次加载会扫描并索引项目结构，请稍候。</p>
            </div>
          ) : error ? (
            <div className="state-card state-card--error">
              <span className="state-error-mark" aria-hidden="true">!</span>
              <div className="eyebrow">LOCAL SERVICE ERROR</div>
              <h2>无法加载项目结构</h2>
              <p className="state-error-detail">{error}</p>
              <button
                type="button"
                className="primary-button state-retry"
                onClick={() => setReloadKey((key) => key + 1)}
              >
                重试
              </button>
              <p className="state-hint">
                确认从 <code>mini-bot web</code> 启动，并且浏览器访问的是同一服务地址。
              </p>
            </div>
          ) : (
            <div className="state-card">
              <h2>尚未加载任何项目</h2>
            </div>
          )}
        </main>
      </div>
    );
  }

  const project = data.project;
  const stats = data.stats;
  const containerNode = getNode(index, containerId);
  const currentLabel = containerNode?.label ?? project.name;
  const currentNodeKind = containerNode
    ? KIND_LABELS[containerNode.kind]
    : "项目";
  const inspectorNode = view.inspectorNode;
  const childSummary = inspectorNode
    ? getChildSummary(index, inspectorNode)
    : "";

  const handleNodeClick = (nodeId: string) => {
    const node = getNode(index, nodeId);
    if (!node) {
      return;
    }
    if (getChildren(index, nodeId).length > 0) {
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
    const current = getNode(index, containerId);
    setContainerId(current?.parentId ?? null);
    setSelectedId(null);
  };

  const handleBreadcrumb = (nodeId: string | null) => {
    setContainerId(nodeId);
    setSelectedId(null);
  };

  return (
    <div className="app-shell">
      {header}

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
              <span
                className={`context-dot kind-${containerNode?.kind ?? "project"}`}
              />
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
                childrenByParent={index.childrenByParent}
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
          node={inspectorNode}
          selected={selectedId !== null}
          project={project}
          stats={stats}
          childSummary={childSummary}
          onClearSelection={() => setSelectedId(null)}
        />
      </main>

      <footer className="status-bar">
        <span>
          <span className="status-dot status-dot--green" />
          固定 Workspace：{project.root}
        </span>
        <span className="status-bar__right">
          <span>FastAPI · React Flow · Dagre</span>
          <span className="status-bar__divider">/</span>
          <span>真实 AST 索引</span>
          <span className="status-bar__divider">/</span>
          <span>
            {stats.modules + stats.classes + stats.functions + stats.methods} 个符号
          </span>
        </span>
      </footer>
    </div>
  );
}
