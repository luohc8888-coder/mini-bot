import { MarkerType, type Edge, type Node } from "@xyflow/react";
import type {
  ArchitectureEdge,
  ArchitectureGraph,
  ArchitectureNode,
  ArchitectureNodeKind,
  ArchitectureSourceRef,
  ArchitectureView,
} from "../types/architecture";

export interface RuntimeNodeData extends Record<string, unknown> {
  label: string;
  kind: ArchitectureNodeKind;
  kindLabel: string;
  description: string;
  meta: string;
  detailView?: string;
  nested: NonNullable<ArchitectureNode["nested"]>;
  sourceRefs: ArchitectureSourceRef[];
  mock: boolean;
  onNestedClick?: (detailView: string) => void;
  onSourceOpen?: (source: ArchitectureSourceRef) => void;
}

export type RuntimeFlowNode = Node<RuntimeNodeData, "runtime">;

export type RelationMode = "main" | "all" | "focus";

export type RuntimeRelation =
  | "main"
  | "branch"
  | "data"
  | "dependency"
  | "other";

export interface RuntimeEdgePoint {
  x: number;
  y: number;
}

export interface RuntimeEdgeRoute {
  points: RuntimeEdgePoint[];
  sourceHandle?: string;
  targetHandle?: string;
}

export interface RuntimeEdgeData extends Record<string, unknown> {
  points: RuntimeEdgePoint[];
  label?: string;
  relation: RuntimeRelation;
  inferred: boolean;
  feedback: boolean;
  selected: boolean;
  dimmed: boolean;
  labelVisible: boolean;
}

export type RuntimeFlowEdge = Edge<RuntimeEdgeData, "orthogonal">;

const KIND_LABELS: Record<ArchitectureNodeKind, string> = {
  component: "逻辑组件",
  stage: "执行阶段",
  condition: "判断条件",
};

const DEFAULT_SIZES: Record<ArchitectureNodeKind, { width: number; height: number }> = {
  component: { width: 360, height: 150 },
  stage: { width: 220, height: 128 },
  condition: { width: 190, height: 118 },
};

const EDGE_COLORS: Record<RuntimeRelation, string> = {
  main: "#7aa7ff",
  branch: "#f2c94c",
  data: "#35d4a4",
  dependency: "#b88cff",
  other: "#8b9bb4",
};

export function runtimeNodeSize(node: ArchitectureNode): {
  width: number;
  height: number;
} {
  const base = node.size ?? DEFAULT_SIZES[node.kind];
  if (node.kind !== "component") {
    return base;
  }
  const nestedCount = node.nested?.length ?? 0;
  return {
    width: base.width,
    height: Math.max(base.height, 150 + nestedCount * 48),
  };
}

export function buildRuntimeFlowNode(
  node: ArchitectureNode,
  mock: boolean,
  onNestedClick: (detailView: string) => void,
  onSourceOpen?: (source: ArchitectureSourceRef) => void,
): RuntimeFlowNode {
  const size = runtimeNodeSize(node);
  return {
    id: node.id,
    type: "runtime",
    position: { x: 0, y: 0 },
    data: {
      label: node.label,
      kind: node.kind,
      kindLabel: KIND_LABELS[node.kind],
      description: node.description,
      meta: node.meta ?? "",
      detailView: node.detailView,
      nested: node.nested ?? [],
      sourceRefs: node.sourceRefs ?? [],
      mock,
      onNestedClick,
      onSourceOpen,
    },
    style: {
      width: size.width,
      height: size.height,
    },
  };
}

/**
 * 兼容旧布局的固定网格。
 *
 * ELK layout 加载或计算失败时仍可回退到这里。真实 ArchitectureGraph
 * 没有 position 字段时，按节点数组顺序排列成 2/3/4 列网格。
 */
export function buildRuntimeFlowNodes(
  view: ArchitectureView,
  mock: boolean,
  onNestedClick: (detailView: string) => void,
  onSourceOpen?: (source: ArchitectureSourceRef) => void,
): RuntimeFlowNode[] {
  const columnCount =
    view.nodes.length <= 4 ? 2 : view.nodes.length <= 9 ? 3 : 4;
  const cellWidth = 440;
  const cellHeight = 300;
  return view.nodes.map((node, index) => ({
    ...buildRuntimeFlowNode(node, mock, onNestedClick, onSourceOpen),
    position: node.position ?? {
      x: (index % columnCount) * cellWidth,
      y: Math.floor(index / columnCount) * cellHeight,
    },
  }));
}

export function classifyArchitectureEdge(edge: ArchitectureEdge): RuntimeRelation {
  switch (edge.kind) {
    case "flow":
      return "main";
    case "branch":
      return "branch";
    case "data":
      return "data";
    case "dependency":
      return "dependency";
    default:
      return "other";
  }
}

/**
 * 保守的关系显示分类：
 * - main：flow + branch（明确属于执行语义）
 * - all：所有关系
 * - focus：所有关系，选中节点后通过边透明度聚焦
 *
 * data/dependency 及未知类型不在默认主流程中显示，只出现在“全部关系”。
 */
export function isEdgeVisible(edge: ArchitectureEdge, mode: RelationMode): boolean {
  if (mode === "main") {
    const relation = classifyArchitectureEdge(edge);
    return relation === "main" || relation === "branch";
  }
  return true;
}

export function isEdgeRelatedToNode(
  edge: ArchitectureEdge,
  nodeId: string | null | undefined,
): boolean {
  return Boolean(nodeId && (edge.source === nodeId || edge.target === nodeId));
}

export interface RuntimeEdgeBuildOptions {
  relationMode?: RelationMode;
  selectedId?: string | null;
  route?: RuntimeEdgeRoute;
  feedback?: boolean;
}

/**
 * 用有向图拓扑保守探测回退/反馈边：只把 DFS 中回到当前递归栈的边标记为
 * feedback。它不依赖节点名称，也不改变 ArchitectureGraph 数据。
 */
export function detectFeedbackEdgeIds(edges: ArchitectureEdge[]): Set<string> {
  const adjacency = new Map<string, Array<{ target: string; edgeId: string }>>();
  for (const edge of edges) {
    const next = adjacency.get(edge.source) ?? [];
    next.push({ target: edge.target, edgeId: edge.id });
    adjacency.set(edge.source, next);
  }

  const visiting = new Set<string>();
  const visited = new Set<string>();
  const feedback = new Set<string>();

  const visit = (nodeId: string): void => {
    if (visiting.has(nodeId)) {
      return;
    }
    if (visited.has(nodeId)) {
      return;
    }
    visiting.add(nodeId);
    for (const next of adjacency.get(nodeId) ?? []) {
      if (visiting.has(next.target)) {
        feedback.add(next.edgeId);
        continue;
      }
      visit(next.target);
    }
    visiting.delete(nodeId);
    visited.add(nodeId);
  };

  for (const edge of edges) {
    visit(edge.source);
  }
  return feedback;
}

export function buildRuntimeFlowEdge(
  edge: ArchitectureEdge,
  options: RuntimeEdgeBuildOptions = {},
): RuntimeFlowEdge {
  const relation = classifyArchitectureEdge(edge);
  const selected = isEdgeRelatedToNode(edge, options.selectedId);
  const dimmed = Boolean(options.selectedId) && !selected;
  const inferred = edge.confidence === "inferred";
  const feedback = Boolean(options.feedback);
  const color = feedback ? "#f2994a" : EDGE_COLORS[relation];
  const isMain = relation === "main";
  const dash =
    feedback
      ? "2 4"
      : relation === "branch"
        ? "5 4"
        : relation === "data"
          ? "7 5"
          : relation === "dependency"
            ? "3 5"
            : relation === "other"
              ? "4 5"
              : inferred
                ? "6 5"
                : undefined;

  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    type: "orthogonal",
    sourceHandle: options.route?.sourceHandle ?? "source-right",
    targetHandle: options.route?.targetHandle ?? "target-left",
    animated: inferred && !dimmed,
    markerEnd: {
      type: MarkerType.ArrowClosed,
      width: 15,
      height: 15,
      color,
    },
    style: {
      stroke: color,
      strokeWidth: selected
        ? isMain
          ? 2.8
          : 1.9
        : isMain
          ? 2.2
          : relation === "branch"
            ? 1.8
            : relation === "data"
              ? 1.4
              : 1.2,
      strokeDasharray: dash,
      opacity: dimmed
        ? 0.12
        : selected
          ? 1
          : relation === "data"
            ? 0.72
            : relation === "dependency"
              ? 0.58
              : 1,
    },
    data: {
      points: options.route?.points ?? [],
      label: edge.label,
      relation,
      inferred,
      feedback,
      selected,
      dimmed,
      labelVisible:
        Boolean(edge.label) &&
        !dimmed &&
        (options.relationMode === "main"
          ? relation === "main" || relation === "branch"
          : true),
    },
  };
}

export function runtimeGraphSummary(graph: ArchitectureGraph, view: ArchitectureView): string {
  const componentCount = view.nodes.filter((node) => node.kind === "component").length;
  const conditionCount = view.nodes.filter((node) => node.kind === "condition").length;
  return `${componentCount} 个组件 · ${conditionCount} 个条件 · ${graph.mock ? "Mock 数据" : "已校验数据"}`;
}
