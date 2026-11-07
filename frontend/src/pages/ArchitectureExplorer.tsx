import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { AppHeader, type ArchitectureBadgeMode } from "../components/AppHeader";
import { Breadcrumb } from "../components/Breadcrumb";
import { RuntimeCanvas } from "../components/RuntimeCanvas";
import { RuntimeInspector } from "../components/RuntimeInspector";
import { SourceChatPanel } from "../components/SourceChatPanel";
import { SourceWorkspace } from "../components/SourceWorkspace";
import {
  architectureErrorMessage,
  fetchArchitectureGraph,
  fetchArchitectureStatus,
  generateArchitecture,
  type ArchitectureStatus,
} from "../architecture/api";
import {
  findArchitectureNode,
  getArchitectureView,
} from "../architecture/graph";
import {
  isEdgeRelatedToNode,
  isEdgeVisible,
  type RelationMode,
} from "../architecture/layout";
import type {
  ArchitectureGraph,
  ArchitectureSourceRef,
  ArchitectureView,
  ExplorerViewMode,
} from "../types/architecture";
import type { BreadcrumbItem } from "../types/graph";

interface ArchitectureExplorerProps {
  workspaceRoot: string;
  onViewChange: (viewMode: ExplorerViewMode) => void;
}

interface ViewStackItem {
  viewId: string;
  label: string;
}

interface SourceGroup {
  label: string;
  refs: ArchitectureSourceRef[];
}

type AnalysisSource = "cache" | "generated";

function buildRuntimeBreadcrumbs(stack: ViewStackItem[]): BreadcrumbItem[] {
  return stack.map((item, index) => ({
    id: item.viewId,
    label: index === 0 ? "运行架构" : item.label,
    kind: index === 0 ? "project" : "component",
    isCurrent: index === stack.length - 1,
    clickable: index < stack.length - 1,
  }));
}

function sourceDisplayLabel(source: ArchitectureSourceRef): string {
  if (source.symbolName) {
    return source.symbolName;
  }
  if (source.qualifiedName) {
    const parts = source.qualifiedName.split(".").filter(Boolean);
    return parts[parts.length - 1] ?? source.qualifiedName;
  }
  return source.filePath ?? "核心源码";
}

function buildSourceBreadcrumbs(
  items: BreadcrumbItem[],
  source: ArchitectureSourceRef | null,
): BreadcrumbItem[] {
  if (!source) {
    return items;
  }
  const base = items.map((item, index) => ({
    ...item,
    isCurrent: false,
    clickable: item.clickable || index === items.length - 1,
  }));
  return [
    ...base,
    {
      id: "source",
      label: sourceDisplayLabel(source),
      kind: "component",
      isCurrent: true,
      clickable: false,
    },
  ];
}

const EMPTY_BREADCRUMBS: BreadcrumbItem[] = [
  {
    id: "overview",
    label: "运行架构",
    kind: "project",
    isCurrent: true,
    clickable: false,
  },
];

export function ArchitectureExplorer({
  workspaceRoot,
  onViewChange,
}: ArchitectureExplorerProps) {
  const [graph, setGraph] = useState<ArchitectureGraph | null>(null);
  const [status, setStatus] = useState<ArchitectureStatus | null>(null);
  const [stack, setStack] = useState<ViewStackItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [sourceRef, setSourceRef] = useState<ArchitectureSourceRef | null>(
    null,
  );
  const [sourceGroup, setSourceGroup] = useState<SourceGroup | null>(null);
  const [relationMode, setRelationMode] = useState<RelationMode>("main");
  const [loading, setLoading] = useState(true);
  const [analyzing, setAnalyzing] = useState(false);
  const [analysisSource, setAnalysisSource] = useState<AnalysisSource | null>(
    null,
  );
  const [staleCache, setStaleCache] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const startedRef = useRef(false);
  const nodeClickTimerRef = useRef<number | null>(null);

  const clearNodeClickTimer = useCallback(() => {
    if (nodeClickTimerRef.current !== null) {
      window.clearTimeout(nodeClickTimerRef.current);
      nodeClickTimerRef.current = null;
    }
  }, []);

  useEffect(() => clearNodeClickTimer, [clearNodeClickTimer]);

  const closeSource = useCallback(() => {
    setSourceGroup(null);
    setSourceRef(null);
  }, []);

  const openSourceGroup = useCallback(
    (
      refs: ArchitectureSourceRef[],
      label: string,
      activeSource?: ArchitectureSourceRef | null,
    ) => {
      const usable = refs.filter((source) => Boolean(source.symbolId));
      if (usable.length === 0) {
        return;
      }
      const selected =
        (activeSource?.symbolId
          ? usable.find(
              (source) => source.symbolId === activeSource.symbolId,
            )
          : null) ?? usable[0];
      setSourceGroup({ label, refs: usable });
      setSourceRef(selected);
    },
    [],
  );

  const applyGraph = useCallback((nextGraph: ArchitectureGraph) => {
    setGraph(nextGraph);
    setStack([
      {
        viewId: nextGraph.defaultViewId,
        label: "运行架构",
      },
    ]);
    setSelectedId(null);
    closeSource();
  }, [closeSource]);

  const runGeneration = useCallback(
    async (force: boolean) => {
      setAnalyzing(true);
      setError(null);
      try {
        const result = await generateArchitecture(force);
        applyGraph(result.graph);
        setAnalysisSource(result.cached ? "cache" : "generated");
        setStaleCache(false);
        const generatedAt = new Date().toISOString();
        setStatus((current) =>
          current
            ? {
                ...current,
                available: true,
                cacheStatus: "valid",
                generatedAt,
                cacheGeneratedAt: generatedAt,
              }
            : current,
        );
      } catch (err) {
        setError(architectureErrorMessage(err));
      } finally {
        setAnalyzing(false);
        setLoading(false);
      }
    },
    [applyGraph],
  );

  const loadStaleCache = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const staleGraph = await fetchArchitectureGraph(true);
      applyGraph(staleGraph);
      setAnalysisSource("cache");
      setStaleCache(true);
    } catch (err) {
      setError(architectureErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [applyGraph]);

  useEffect(() => {
    if (startedRef.current) {
      return;
    }
    startedRef.current = true;
    let cancelled = false;

    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const nextStatus = await fetchArchitectureStatus();
        if (cancelled) {
          return;
        }
        setStatus(nextStatus);

        const cacheStatus =
          nextStatus.cacheStatus ??
          (nextStatus.available ? "valid" : "missing");
        if (cacheStatus !== "valid") {
          return;
        }

        try {
          const cachedGraph = await fetchArchitectureGraph();
          if (!cancelled) {
            applyGraph(cachedGraph);
            setAnalysisSource("cache");
            setStaleCache(false);
          }
        } catch (loadError) {
          if (cancelled) {
            return;
          }
          setError(architectureErrorMessage(loadError));
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(architectureErrorMessage(loadError));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
          setAnalyzing(false);
        }
      }
    };

    void load();
    return () => {
      cancelled = true;
      startedRef.current = false;
    };
  }, [applyGraph]);

  const current = stack[stack.length - 1];
  const currentView =
    graph && current ? getArchitectureView(graph, current.viewId) : null;
  const selectedNode =
    graph && currentView
      ? findArchitectureNode(graph, currentView.id, selectedId)
      : null;
  const breadcrumbs = useMemo(
    () => buildSourceBreadcrumbs(buildRuntimeBreadcrumbs(stack), sourceRef),
    [stack, sourceRef],
  );
  const visibleEdgeCount = currentView
    ? currentView.edges.filter((edge) => isEdgeVisible(edge, relationMode)).length
    : 0;

  const handleSourceSelect = useCallback(
    (source: ArchitectureSourceRef) => {
      clearNodeClickTimer();
      if (!source.symbolId) {
        return;
      }
      const ownerNode =
        graph && currentView
          ? currentView.nodes.find((node) =>
              (node.sourceRefs ?? []).some(
                (ref) => ref.symbolId === source.symbolId,
              ),
            )
          : null;
      if (ownerNode) {
        setSelectedId(ownerNode.id);
        openSourceGroup(ownerNode.sourceRefs ?? [source], ownerNode.label, source);
        return;
      }
      openSourceGroup([source], sourceDisplayLabel(source), source);
    },
    [currentView, graph, openSourceGroup, clearNodeClickTimer],
  );

  const openView = (view: ArchitectureView, label: string) => {
    setStack((previous) => {
      const active = previous[previous.length - 1];
      if (active?.viewId === view.id) {
        return previous;
      }
      return [...previous, { viewId: view.id, label }];
    });
    setSelectedId(null);
  };

  const handleNodeClick = (nodeId: string) => {
    if (!graph || !currentView) {
      return;
    }
    const node = findArchitectureNode(graph, currentView.id, nodeId);
    if (!node) {
      return;
    }
    clearNodeClickTimer();
    setSelectedId(node.id);
    const sourceRefs = (node.sourceRefs ?? []).filter(
      (source) => source.symbolId,
    );
    if (sourceRefs.length > 0) {
      // 有下钻视图的节点仍保留双击下钻；单击稍等片刻再进入源码，
      // 避免双击时第一下就被源码工作台拦截。
      if (node.detailView) {
        nodeClickTimerRef.current = window.setTimeout(() => {
          nodeClickTimerRef.current = null;
          openSourceGroup(sourceRefs, node.label, sourceRefs[0]);
        }, 260);
      } else {
        openSourceGroup(sourceRefs, node.label, sourceRefs[0]);
      }
      return;
    }
    if (
      relationMode === "main" &&
      currentView.edges.some(
        (edge) =>
          isEdgeRelatedToNode(edge, node.id) &&
          !isEdgeVisible(edge, "main"),
      )
    ) {
      setRelationMode("focus");
    }
  };

  const handleNodeDoubleClick = (nodeId: string) => {
    clearNodeClickTimer();
    if (!graph || !currentView) {
      return;
    }
    const node = findArchitectureNode(graph, currentView.id, nodeId);
    if (!node?.detailView) {
      return;
    }
    const targetView = graph.views[node.detailView];
    if (targetView) {
      openView(targetView, node.label);
    }
  };

  const handleNestedClick = useCallback(
    (detailView: string) => {
      const targetView = graph?.views[detailView];
      if (!targetView) {
        return;
      }
      const nested = currentView?.nodes
        .flatMap((node) => node.nested ?? [])
        .find((node) => node.detailView === detailView);
      openView(targetView, nested?.label ?? targetView.title);
    },
    [currentView, graph],
  );

  const handleBreadcrumb = (viewId: string | null) => {
    clearNodeClickTimer();
    closeSource();
    if (!viewId) {
      return;
    }
    const targetIndex = stack.findIndex((item) => item.viewId === viewId);
    if (targetIndex < 0) {
      return;
    }
    setStack(stack.slice(0, targetIndex + 1));
    setSelectedId(null);
  };

  const handleBack = () => {
    clearNodeClickTimer();
    if (sourceRef || sourceGroup) {
      closeSource();
      return;
    }
    if (stack.length <= 1) {
      return;
    }
    setStack(stack.slice(0, -1));
    setSelectedId(null);
  };

  const badgeMode: ArchitectureBadgeMode = graph
    ? graph.mock
      ? "mock"
      : "live"
    : analyzing
      ? "loading"
      : status?.configured
        ? "ready"
        : status
          ? "unconfigured"
          : "loading";

  const subtitle = graph
    ? graph.mock
      ? "AI 驱动的项目架构理解工作台 · Mock"
      : "AI 驱动的项目架构理解工作台 · Phase 4.3"
    : analyzing
      ? "AI 正在读取结构化索引并生成运行架构…"
      : "AI 驱动的项目架构理解工作台 · Phase 4.3";


  return (
    <div className="app-shell">
      <AppHeader
        workspaceRoot={workspaceRoot}
        viewMode="architecture"
        onViewChange={onViewChange}
        subtitle={subtitle}
        architectureMode={badgeMode}
      />

      <main
        className={`workspace${sourceRef ? " workspace--source-mode" : ""}`}
      >
        <section className="graph-column">
          <div className="graph-toolbar">
            <button
              type="button"
              className="back-button"
              onClick={handleBack}
              disabled={!sourceRef && stack.length <= 1}
              title={sourceRef ? "返回架构图" : "返回上一级架构视图"}
            >
              <span aria-hidden="true">←</span>
              {sourceRef ? "返回架构" : "返回"}
            </button>

            <div className="toolbar-divider" aria-hidden="true" />

            <Breadcrumb
              items={sourceRef || currentView ? breadcrumbs : EMPTY_BREADCRUMBS}
              onNavigate={handleBreadcrumb}
            />

            <div className="toolbar-spacer" />

            <div className="view-context">
              <span className="context-dot kind-runtime-architecture" />
              <span>
                {sourceRef
                  ? sourceDisplayLabel(sourceRef)
                  : currentView?.title ??
                    (analyzing ? "AI 分析中" : "运行架构")}
              </span>
              <span className="view-context__kind">
                {sourceRef
                  ? "核心源码"
                  : graph?.mock
                    ? "Mock"
                    : graph
                      ? "LLM 架构"
                      : status?.cacheStatus === "stale"
                        ? "缓存已过期"
                        : "等待生成"}
              </span>
            </div>
            {currentView && !sourceRef ? (
              <div className="relation-mode-switch" role="group" aria-label="关系显示模式">
                <button
                  type="button"
                  className={`relation-mode-button${
                    relationMode === "main" ? " is-active" : ""
                  }`}
                  aria-label="主执行流程"
                  aria-pressed={relationMode === "main"}
                  title="主执行流程：仅显示 flow / branch 关系"
                  onClick={() => setRelationMode("main")}
                >
                  主流程
                </button>
                <button
                  type="button"
                  className={`relation-mode-button${
                    relationMode === "all" ? " is-active" : ""
                  }`}
                  aria-label="全部关系"
                  aria-pressed={relationMode === "all"}
                  title="全部关系：显示 ArchitectureGraph 中的所有关系"
                  onClick={() => setRelationMode("all")}
                >
                  全部
                </button>
                <button
                  type="button"
                  className={`relation-mode-button${
                    relationMode === "focus" ? " is-active" : ""
                  }`}
                  aria-label="节点聚焦"
                  aria-pressed={relationMode === "focus"}
                  title="节点聚焦：单击节点后高亮直接上下游关系"
                  onClick={() => setRelationMode("focus")}
                >
                  聚焦
                </button>
              </div>
            ) : null}
            {currentView && !sourceRef ? (
              <span className="view-stats">
                {currentView.nodes.length} 节点 · {visibleEdgeCount}/
                {currentView.edges.length} 关系
              </span>
            ) : null}
            {!sourceRef ? (
              <button
                type="button"
                className="ghost-button architecture-regenerate"
                onClick={() => void runGeneration(true)}
                disabled={!status?.configured || analyzing}
                title={
                  status?.configured
                    ? "忽略缓存并重新调用 LLM 分析"
                    : "尚未配置 LLM Provider"
                }
              >
                {analyzing ? "分析中…" : "重新生成"}
              </button>
            ) : null}
          </div>

          <div
            className={`graph-stage runtime-stage${
              sourceRef ? " runtime-stage--source-open" : ""
            }`}
          >
            <div
              className={`runtime-notice${
                graph && !graph.mock ? " runtime-notice--live" : ""
              }`}
            >
              <span className="runtime-notice__mark" aria-hidden="true">
                ✦
              </span>
              <span className="runtime-notice__title">
                {analyzing
                  ? "ANALYZING"
                  : graph?.mock
                    ? "MOCK ARCHITECTURE"
                    : graph
                      ? "AI ARCHITECTURE"
                      : "NO ARCHITECTURE"}
              </span>
              <span className="runtime-notice__copy">
                {analyzing
                  ? "LLM 正在读取结构化索引与源码窗口"
                  : graph?.mock
                    ? "Phase 4.1 · 不调用 LLM"
                    : graph
                      ? "Phase 4.3 · SymbolIndex 已验证"
                      : "尚未生成可交互的运行架构"}
              </span>
            </div>

            {staleCache ? (
              <div className="runtime-cache-banner" role="status">
                <span className="runtime-cache-banner__title">旧缓存</span>
                <span className="runtime-cache-banner__copy">
                  源码指纹已变化，当前展示旧架构；需要最新结构请重新生成。
                </span>
              </div>
            ) : null}

            {error ? (
              <div className="architecture-error" role="alert">
                <div>
                  <strong>架构分析失败</strong>
                  <span>{error}</span>
                </div>
                <button
                  type="button"
                  className="ghost-button"
                  onClick={() => void runGeneration(true)}
                  disabled={!status?.configured || analyzing}
                >
                  重试
                </button>
              </div>
            ) : null}

            {currentView ? (
              <>
                <div className="graph-hints graph-hints--runtime">
                  <span>单击选中 · 双击组件下钻</span>
                  <span className="hint-separator">·</span>
                  <span>拖拽画布</span>
                  <span className="hint-separator">·</span>
                  <span>滚轮缩放</span>
                </div>

                <ReactFlowProvider>
                  <RuntimeCanvas
                    view={currentView}
                    graphMock={graph?.mock ?? false}
                    selectedId={selectedId}
                    relationMode={relationMode}
                    onNodeClick={handleNodeClick}
                    onNodeDoubleClick={handleNodeDoubleClick}
                    onNestedClick={handleNestedClick}
                    onSourceOpen={handleSourceSelect}
                    onPaneClick={() => {
                      clearNodeClickTimer();
                      setSelectedId(null);
                      closeSource();
                    }}
                  />
                </ReactFlowProvider>

                {sourceRef && sourceGroup ? (
                  <SourceWorkspace
                    key={`${sourceGroup.label}:${sourceGroup.refs
                      .map((ref) => ref.symbolId ?? ref.filePath ?? "")
                      .join("|")}`}
                    sources={sourceGroup.refs}
                    activeSymbolId={sourceRef.symbolId ?? null}
                    title={sourceGroup.label}
                    onSelectSource={setSourceRef}
                    onClose={closeSource}
                  />
                ) : null}

                <div className="graph-legend runtime-legend" aria-label="运行架构图例">
                  <span className="legend-group">
                    <i className="legend-dot legend-dot--component" />
                    逻辑组件
                  </span>
                  <span className="legend-group">
                    <i className="legend-dot legend-dot--stage" />
                    执行阶段
                  </span>
                  <span className="legend-group">
                    <i className="legend-dot legend-dot--condition" />
                    条件分支
                  </span>
                  <span className="legend-divider" />
                  <span className="legend-group legend-group--muted">
                    <i className="legend-line legend-line--flow" />
                    执行流
                  </span>
                  <span className="legend-group legend-group--muted">
                    <i className="legend-line legend-line--data" />
                    数据流
                  </span>
                  <span className="legend-group legend-group--muted">
                    <i className="legend-line legend-line--branch" />
                    分支
                  </span>
                  <span className="legend-group legend-group--muted">
                    <i className="legend-line legend-line--dependency" />
                    依赖
                  </span>
                  <span className="legend-group legend-group--muted">
                    <i className="legend-line legend-line--feedback" />
                    反馈
                  </span>
                </div>
              </>
            ) : (
              <ArchitectureEmptyState
                loading={loading}
                analyzing={analyzing}
                status={status}
                error={error}
                onGenerate={() => void runGeneration(false)}
                onUseStale={
                  status?.cacheStatus === "stale"
                    ? () => void loadStaleCache()
                    : undefined
                }
              />
            )}
          </div>
        </section>

        {sourceRef && sourceGroup ? (
          <SourceChatPanel
            key={sourceRef.symbolId ?? sourceRef.filePath ?? "source-chat"}
            source={sourceRef}
            nodeLabel={sourceGroup.label}
            configured={Boolean(status?.configured)}
          />
        ) : graph && currentView ? (
          <RuntimeInspector
            node={selectedNode}
            view={currentView}
            graph={graph}
            selected={selectedId !== null}
            onClearSelection={() => setSelectedId(null)}
            onSourceSelect={handleSourceSelect}
            activeSourceId={null}
            analysis={
              graph.mock
                ? null
                : {
                    provider: status?.provider ?? "",
                    model: status?.model ?? "",
                    source: analysisSource,
                  }
            }
          />
        ) : (
          <ArchitectureEmptyInspector
            workspaceRoot={workspaceRoot}
            status={status}
            loading={loading}
            analyzing={analyzing}
            error={error}
          />
        )}
      </main>

      <footer className="status-bar">
        <span>
          <span
            className={`status-dot${
              graph && !graph.mock ? " status-dot--green" : ""
            }`}
          />
          固定 Workspace：{workspaceRoot}
        </span>
        <span className="status-bar__right">
          <span>
            {graph
              ? graph.mock
                ? "Mock ArchitectureGraph"
                : "LLM ArchitectureGraph"
              : "ArchitectureGraph 未生成"}
          </span>
          <span className="status-bar__divider">/</span>
          <span>{status?.provider || "Provider 未配置"}</span>
          <span className="status-bar__divider">/</span>
          <span>
            {staleCache
              ? "旧缓存"
              : analysisSource === "cache"
                ? "缓存结果"
                : graph
                  ? "本次生成"
                  : "无 LLM 调用"}
          </span>
        </span>
      </footer>
    </div>
  );
}

interface ArchitectureEmptyStateProps {
  loading: boolean;
  analyzing: boolean;
  status: ArchitectureStatus | null;
  error: string | null;
  onGenerate: () => void;
  onUseStale?: () => void;
}

function ArchitectureEmptyState({
  loading,
  analyzing,
  status,
  error,
  onGenerate,
  onUseStale,
}: ArchitectureEmptyStateProps) {
  const busy = loading || analyzing;
  const canGenerate = Boolean(status?.configured) && !busy;
  const stale = status?.cacheStatus === "stale";
  const canUseStale = Boolean(onUseStale) && !busy;
  return (
    <div className="architecture-empty">
      <span className="architecture-empty__mark" aria-hidden="true">
        ✦
      </span>
      <h3>
        {analyzing
          ? "正在分析项目架构"
          : loading
            ? "正在加载运行架构"
            : stale
              ? "架构缓存已过期"
              : "还没有生成运行架构"}
      </h3>
      <p>
        {analyzing
          ? "LLM 正在读取有界的项目索引、关键符号和源码窗口；所有源码引用都会经过 SymbolIndex 校验。"
          : loading
            ? "正在检查本地缓存与 Provider 状态。"
            : stale
              ? "检测到项目源码发生变化，上次生成的架构缓存已失效。可以继续查看旧缓存，或重新分析项目。"
              : status?.configured
                ? "点击下方按钮，让 LLM 基于 AST/CodeGraph 生成可交互的语义架构图。"
                : "运行架构需要先在 mini-bot 中配置 LLM Provider。代码结构浏览不依赖 LLM。"}
      </p>
      {stale && status?.cacheGeneratedAt ? (
        <div className="architecture-empty__hint">
          上次缓存生成时间：{status.cacheGeneratedAt}
        </div>
      ) : null}
      {status && !status.configured ? (
        <div className="architecture-empty__hint">
          {status.error ?? "请先运行 mini-bot 完成 Provider 配置。"}
        </div>
      ) : null}
      {error ? <div className="architecture-empty__error">{error}</div> : null}
      {busy ? (
        <div className="architecture-progress">
          <span />
          正在工作…
        </div>
      ) : stale ? (
        <div className="architecture-empty__actions">
          <button
            type="button"
            className="ghost-button architecture-empty__action"
            onClick={onUseStale}
            disabled={!canUseStale}
          >
            使用旧缓存
          </button>
          <button
            type="button"
            className="primary-button architecture-empty__action"
            onClick={onGenerate}
            disabled={!canGenerate}
          >
            重新分析
          </button>
        </div>
      ) : (
        <button
          type="button"
          className="primary-button architecture-empty__action"
          onClick={onGenerate}
          disabled={!canGenerate}
        >
          生成运行架构
        </button>
      )}
    </div>
  );
}

interface ArchitectureEmptyInspectorProps {
  workspaceRoot: string;
  status: ArchitectureStatus | null;
  loading: boolean;
  analyzing: boolean;
  error: string | null;
}

function ArchitectureEmptyInspector({
  workspaceRoot,
  status,
  loading,
  analyzing,
  error,
}: ArchitectureEmptyInspectorProps) {
  return (
    <aside className="inspector runtime-inspector">
      <div className="inspector__header">
        <div>
          <div className="eyebrow">ARCHITECTURE INSPECTOR</div>
          <h2 className="inspector__title">{workspaceRoot}</h2>
        </div>
      </div>
      <div className="inspector__badges">
        <span className="kind-pill kind-runtime-component">项目架构</span>
        <span className={`soft-pill${status?.configured ? "" : " soft-pill--warn"}`}>
          {analyzing || loading
            ? "检查中"
            : status?.configured
              ? "LLM Ready"
              : "LLM 未配置"}
        </span>
      </div>
      <p className="inspector__summary">
        运行架构由 Phase 4.2 的结构化分析输入驱动，通过 LLM 生成语义组件与执行链，
        再使用 SymbolIndex 校验源码引用。
      </p>
      <div className="meta-grid">
        <div className="meta-grid__item">
          <span>Provider</span>
          <strong>{status?.provider || "未配置"}</strong>
        </div>
        <div className="meta-grid__item">
          <span>模型</span>
          <strong>{status?.model || "—"}</strong>
        </div>
        <div className="meta-grid__item meta-grid__item--wide">
          <span>缓存状态</span>
          <strong>
            {status?.cacheStatus === "valid"
              ? "有效缓存"
              : status?.cacheStatus === "stale"
                ? "缓存已过期"
                : "尚未生成"}
          </strong>
        </div>
      </div>
      <section className="panel-section">
        <div className="panel-section__title">
          <span>源码引用协议</span>
          <span className="section-tag">SymbolIndex</span>
        </div>
        <div className="ai-reserved">
          <div className="ai-reserved__intro">
            <span className="ai-reserved__spark">✦</span>
            <p>
              LLM 只输出结构化 ArchitectureGraph，不输出 Mermaid；任何无法在
              SymbolIndex 中验证的源码引用都会被拒绝，无法静态确认的关系标记为 inferred。
            </p>
          </div>
        </div>
      </section>
      {error ? <div className="inspector__error">{error}</div> : null}
      <div className="inspector__footer">
        <span className={status?.configured ? "status-dot status-dot--green" : "status-dot status-dot--mock"} />
        <span>
          {status?.configured
            ? "LLM Provider 已就绪 · 只读分析"
            : "等待 Provider 配置 · 代码结构浏览可用"}
        </span>
      </div>
    </aside>
  );
}
