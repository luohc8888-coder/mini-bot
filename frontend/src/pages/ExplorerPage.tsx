import { useCallback, useEffect, useMemo, useState } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { AppHeader } from "../components/AppHeader";
import { Breadcrumb } from "../components/Breadcrumb";
import { GraphCanvas } from "../components/GraphCanvas";
import { InspectorPanel } from "../components/InspectorPanel";
import { QuickSearchModal } from "../components/QuickSearchModal";
import { SourceChatPanel } from "../components/SourceChatPanel";
import { SourceWorkspace } from "../components/SourceWorkspace";
import { ArchitectureExplorer } from "./ArchitectureExplorer";
import { fetchExplorerData } from "../graph/api";
import {
  buildGraphData,
  buildGraphIndex,
  buildViewModel,
  getChildSummary,
  getChildren,
  getNode,
  getNodeRelations,
  type GraphData,
  type GraphIndex,
} from "../graph/data";
import { applyTheme, getInitialTheme } from "../theme";
import type {
  ArchitectureSourceRef,
  ExplorerViewMode,
} from "../types/architecture";
import type { MockNode, NodeKind } from "../types/graph";

const KIND_LABELS: Record<NodeKind, string> = {
  module: "模块",
  class: "类",
  function: "函数",
  method: "方法",
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function nodeToSourceRef(node: MockNode): ArchitectureSourceRef {
  return {
    symbolId: node.id,
    symbolName: node.label,
    qualifiedName: node.qualifiedName,
    filePath: node.filePath,
    startLine: node.startLine,
    endLine: node.endLine,
    verified: true,
  };
}

export function ExplorerPage() {
  const [data, setData] = useState<GraphData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const [containerId, setContainerId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [structureSource, setStructureSource] =
    useState<ArchitectureSourceRef | null>(null);
  const [viewMode, setViewMode] = useState<ExplorerViewMode>("architecture");
  const [searchOpen, setSearchOpen] = useState(false);
  const [aiConfigured, setAiConfigured] = useState(false);

  useEffect(() => {
    applyTheme(getInitialTheme());
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setSearchOpen((prev) => !prev);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

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
      index ? buildViewModel(index, containerId, selectedId) : null,
    [index, containerId, selectedId],
  );

  const openNodeSource = useCallback((node: MockNode) => {
    if (node.synthetic) {
      return;
    }
    setSelectedId(node.id);
    setStructureSource(nodeToSourceRef(node));
  }, []);

  const closeStructureSource = useCallback(() => {
    setStructureSource(null);
  }, []);

  const handleDrillDown = useCallback(
    (nodeId: string) => {
      if (!index) {
        return;
      }
      if (getChildren(index, nodeId).length > 0) {
        setContainerId(nodeId);
        setSelectedId(null);
        setStructureSource(null);
      }
    },
    [index],
  );

  const handleNodeClick = useCallback((nodeId: string) => {
    setSelectedId(nodeId);
  }, []);

  const handleNodeAction = useCallback(
    (nodeId: string) => {
      if (!index) {
        return;
      }
      const node = getNode(index, nodeId);
      if (!node) {
        return;
      }
      if (getChildren(index, nodeId).length > 0) {
        setContainerId(nodeId);
        setSelectedId(null);
        setStructureSource(null);
        return;
      }
      openNodeSource(node);
    },
    [index, openNodeSource],
  );

  const handleNavigateToNode = useCallback(
    (nodeId: string, openSource = false) => {
      if (!index) {
        return;
      }
      const target = getNode(index, nodeId);
      if (!target) {
        return;
      }
      setViewMode("structure");
      setContainerId(target.parentId ?? null);
      setSelectedId(target.id);
      if (openSource && !target.synthetic) {
        setStructureSource(nodeToSourceRef(target));
      } else {
        setStructureSource(null);
      }
    },
    [index],
  );

  const handleLocateFromArchitecture = useCallback(
    (symbolId: string) => {
      handleNavigateToNode(symbolId, false);
    },
    [handleNavigateToNode],
  );

  const handleSearchSelect = useCallback(
    (node: MockNode, openSource: boolean) => {
      setSearchOpen(false);
      handleNavigateToNode(node.id, openSource);
    },
    [handleNavigateToNode],
  );

  const handleBack = () => {
    if (structureSource) {
      setStructureSource(null);
      return;
    }
    if (!containerId || !index) {
      return;
    }
    const current = getNode(index, containerId);
    setContainerId(current?.parentId ?? null);
    setSelectedId(null);
  };

  const handleBreadcrumb = (nodeId: string | null) => {
    setStructureSource(null);
    setContainerId(nodeId);
    setSelectedId(null);
  };

  const project = data?.project ?? null;
  const stats = data?.stats ?? null;
  const containerNode = index ? getNode(index, containerId) : null;
  const currentLabel = containerNode?.label ?? project?.name ?? "代码结构";
  const currentNodeKind = containerNode
    ? containerNode.synthetic
      ? "目录"
      : KIND_LABELS[containerNode.kind]
    : "项目";
  const inspectorNode = view?.inspectorNode ?? null;
  const childSummary =
    index && inspectorNode ? getChildSummary(index, inspectorNode) : "";
  const inspectorHasChildren =
    index && inspectorNode
      ? getChildren(index, inspectorNode.id).length > 0
      : false;
  const inspectorRelations = useMemo(
    () =>
      index && inspectorNode
        ? getNodeRelations(index, inspectorNode.id)
        : { incoming: [], outgoing: [] },
    [index, inspectorNode],
  );

  const isStructureActive = viewMode === "structure";

  return (
    <>
      <ArchitectureExplorer
        workspaceRoot={data?.project.root ?? "当前 Workspace"}
        active={viewMode === "architecture"}
        onViewChange={setViewMode}
        onOpenSearch={() => setSearchOpen(true)}
        onLocateInStructure={handleLocateFromArchitecture}
        onAiConfiguredChange={setAiConfigured}
      />

      <div
        className={`app-shell${isStructureActive ? "" : " view-pane--hidden"}`}
      >
        <AppHeader
          workspaceRoot={project?.root ?? "正在连接本地服务..."}
          viewMode="structure"
          onViewChange={setViewMode}
          onOpenSearch={() => setSearchOpen(true)}
        />

        {!data || !index || !view || !project || !stats ? (
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
                <span className="state-error-mark" aria-hidden="true">
                  !
                </span>
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
                  确认从 <code>mini-bot web</code>{" "}
                  启动，并且浏览器访问的是同一服务地址。
                </p>
              </div>
            ) : (
              <div className="state-card">
                <h2>尚未加载任何项目</h2>
              </div>
            )}
          </main>
        ) : (
          <>
            <main
              className={`workspace${
                structureSource ? " workspace--source-mode" : ""
              }`}
            >
              <section className="graph-column">
                <div className="graph-toolbar">
                  <button
                    type="button"
                    className="back-button"
                    onClick={handleBack}
                    disabled={!structureSource && !containerId}
                    title={structureSource ? "返回代码结构图" : "返回上一级"}
                  >
                    <span aria-hidden="true">←</span>
                    {structureSource ? "返回结构" : "返回"}
                  </button>

                  <div className="toolbar-divider" aria-hidden="true" />

                  <Breadcrumb
                    items={view.breadcrumbs}
                    onNavigate={handleBreadcrumb}
                  />

                  <div className="toolbar-spacer" />

                  <div className="view-context">
                    <span
                      className={`context-dot kind-${
                        containerNode?.kind ?? "project"
                      }`}
                    />
                    <span>
                      {structureSource
                        ? structureSource.symbolName ?? "核心源码"
                        : currentLabel}
                    </span>
                    <span className="view-context__kind">
                      {structureSource ? "源码" : currentNodeKind}
                    </span>
                  </div>
                  <span className="view-stats">
                    {view.visibleNodes.length} 节点 · {view.visibleEdges.length}{" "}
                    关系
                  </span>
                </div>

                <div
                  className={`graph-stage${
                    structureSource ? " runtime-stage--source-open" : ""
                  }`}
                >
                  <div className="graph-hints">
                    <span>单击选中</span>
                    <span className="hint-separator">·</span>
                    <span>双击下钻 / 看源码</span>
                    <span className="hint-separator">·</span>
                    <span>⌘K 全局搜索</span>
                  </div>

                  <ReactFlowProvider>
                    <GraphCanvas
                      viewKey={containerId ?? "root"}
                      nodes={view.visibleNodes}
                      edges={view.visibleEdges}
                      childrenByParent={index.childrenByParent}
                      selectedId={selectedId}
                      onNodeClick={handleNodeClick}
                      onNodeDoubleClick={handleNodeAction}
                      onNodeAction={handleNodeAction}
                      onPaneClick={() => {
                        setSelectedId(null);
                        setStructureSource(null);
                      }}
                    />
                  </ReactFlowProvider>

                  {structureSource ? (
                    <SourceWorkspace
                      sources={[structureSource]}
                      activeSymbolId={structureSource.symbolId ?? null}
                      title={structureSource.symbolName ?? "核心源码"}
                      onSelectSource={setStructureSource}
                      onClose={closeStructureSource}
                    />
                  ) : null}

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

              {structureSource ? (
                <SourceChatPanel
                  source={structureSource}
                  nodeLabel={structureSource.symbolName}
                  configured={aiConfigured}
                />
              ) : (
                <InspectorPanel
                  node={inspectorNode}
                  selected={selectedId !== null}
                  project={project}
                  stats={stats}
                  childSummary={childSummary}
                  hasChildren={inspectorHasChildren}
                  incoming={inspectorRelations.incoming}
                  outgoing={inspectorRelations.outgoing}
                  aiConfigured={aiConfigured}
                  onClearSelection={() => setSelectedId(null)}
                  onDrillDown={handleDrillDown}
                  onOpenSource={openNodeSource}
                  onNavigateNode={(nodeId) => handleNavigateToNode(nodeId, false)}
                />
              )}
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
                  {stats.modules + stats.classes + stats.functions + stats.methods}{" "}
                  个符号
                </span>
              </span>
            </footer>
          </>
        )}
      </div>

      <QuickSearchModal
        open={searchOpen}
        nodes={data?.nodes ?? []}
        onClose={() => setSearchOpen(false)}
        onSelectSymbol={handleSearchSelect}
      />
    </>
  );
}
