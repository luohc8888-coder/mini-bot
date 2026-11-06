import { useCallback, useMemo, useState } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { AppHeader } from "../components/AppHeader";
import { Breadcrumb } from "../components/Breadcrumb";
import { RuntimeCanvas } from "../components/RuntimeCanvas";
import { RuntimeInspector } from "../components/RuntimeInspector";
import {
  findArchitectureNode,
  getArchitectureView,
  mockArchitecture,
} from "../architecture/mockArchitecture";
import type {
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

function buildRuntimeBreadcrumbs(stack: ViewStackItem[]): BreadcrumbItem[] {
  return stack.map((item, index) => ({
    id: item.viewId,
    label: index === 0 ? "运行架构" : item.label,
    kind: index === 0 ? "project" : "component",
    isCurrent: index === stack.length - 1,
    clickable: index < stack.length - 1,
  }));
}

export function ArchitectureExplorer({
  workspaceRoot,
  onViewChange,
}: ArchitectureExplorerProps) {
  const [stack, setStack] = useState<ViewStackItem[]>([
    {
      viewId: mockArchitecture.defaultViewId,
      label: "运行架构",
    },
  ]);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const current = stack[stack.length - 1];
  const currentView = getArchitectureView(mockArchitecture, current.viewId);
  const selectedNode = findArchitectureNode(
    mockArchitecture,
    currentView.id,
    selectedId,
  );
  const breadcrumbs = useMemo(() => buildRuntimeBreadcrumbs(stack), [stack]);

  const openView = (view: ArchitectureView, label: string) => {
    setStack((previous) => {
      const active = previous[previous.length - 1];
      if (active.viewId === view.id) {
        return previous;
      }
      return [...previous, { viewId: view.id, label }];
    });
    setSelectedId(null);
  };

  const handleNodeClick = (nodeId: string) => {
    const node = findArchitectureNode(
      mockArchitecture,
      currentView.id,
      nodeId,
    );
    if (!node) {
      return;
    }
    if (node.detailView) {
      const targetView = mockArchitecture.views[node.detailView];
      if (targetView) {
        openView(targetView, node.label);
      }
      return;
    }
    setSelectedId(node.id);
  };

  const handleNestedClick = useCallback(
    (detailView: string) => {
      const targetView = mockArchitecture.views[detailView];
      if (!targetView) {
        return;
      }
      const nested = currentView.nodes
        .flatMap((node) => node.nested ?? [])
        .find((node) => node.detailView === detailView);
      openView(targetView, nested?.label ?? "内部流程");
    },
    [currentView],
  );

  const handleBreadcrumb = (viewId: string | null) => {
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
    if (stack.length <= 1) {
      return;
    }
    setStack(stack.slice(0, -1));
    setSelectedId(null);
  };

  return (
    <div className="app-shell">
      <AppHeader
        workspaceRoot={workspaceRoot}
        viewMode="architecture"
        onViewChange={onViewChange}
        subtitle="AI 驱动的项目架构理解工作台 · Phase 4.1 Mock"
      />

      <main className="workspace">
        <section className="graph-column">
          <div className="graph-toolbar">
            <button
              type="button"
              className="back-button"
              onClick={handleBack}
              disabled={stack.length <= 1}
              title="返回上一级架构视图"
            >
              <span aria-hidden="true">←</span>
              返回
            </button>

            <div className="toolbar-divider" aria-hidden="true" />

            <Breadcrumb items={breadcrumbs} onNavigate={handleBreadcrumb} />

            <div className="toolbar-spacer" />

            <div className="view-context">
              <span className="context-dot kind-runtime-architecture" />
              <span>{currentView.title}</span>
              <span className="view-context__kind">运行架构</span>
            </div>
            <span className="view-stats">
              {currentView.nodes.length} 节点 · {currentView.edges.length} 关系
            </span>
          </div>

          <div className="graph-stage runtime-stage">
            <div className="runtime-notice">
              <span className="runtime-notice__mark" aria-hidden="true">
                ✦
              </span>
              <span className="runtime-notice__title">MOCK ARCHITECTURE</span>
              <span className="runtime-notice__copy">
                Phase 4.1 · 不调用 LLM / 不修改后端协议
              </span>
            </div>

            <div className="graph-hints graph-hints--runtime">
              <span>点击组件下钻</span>
              <span className="hint-separator">·</span>
              <span>拖拽画布</span>
              <span className="hint-separator">·</span>
              <span>滚轮缩放</span>
            </div>

            <ReactFlowProvider>
              <RuntimeCanvas
                view={currentView}
                graphMock={mockArchitecture.mock}
                selectedId={selectedId}
                onNodeClick={handleNodeClick}
                onNestedClick={handleNestedClick}
                onPaneClick={() => setSelectedId(null)}
              />
            </ReactFlowProvider>

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
            </div>
          </div>
        </section>

        <RuntimeInspector
          node={selectedNode}
          view={currentView}
          graph={mockArchitecture}
          selected={selectedId !== null}
          onClearSelection={() => setSelectedId(null)}
        />
      </main>

      <footer className="status-bar">
        <span>
          <span className="status-dot status-dot--mock" />
          固定 Workspace：{workspaceRoot}
        </span>
        <span className="status-bar__right">
          <span>Mock ArchitectureGraph</span>
          <span className="status-bar__divider">/</span>
          <span>Phase 4.1 原型</span>
          <span className="status-bar__divider">/</span>
          <span>无 LLM 调用</span>
        </span>
      </footer>
    </div>
  );
}
