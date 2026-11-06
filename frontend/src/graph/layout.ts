import dagre from "@dagrejs/dagre";
import { MarkerType, type Edge, type Node } from "@xyflow/react";
import { summarizeChildren } from "./data";
import type { MockEdge, MockNode, NodeKind } from "../types/graph";

export interface ArchitectureNodeData extends Record<string, unknown> {
  label: string;
  kind: NodeKind;
  kindLabel: string;
  meta: string;
  summary: string;
  childSummary: string;
  hasChildren: boolean;
}

export type ArchitectureFlowNode = Node<ArchitectureNodeData, "architecture">;

interface NodeSize {
  width: number;
  height: number;
}

const NODE_SIZES: Record<NodeKind, NodeSize> = {
  module: { width: 254, height: 108 },
  class: { width: 238, height: 98 },
  function: { width: 226, height: 92 },
  method: { width: 214, height: 88 },
};

const KIND_LABELS: Record<NodeKind, string> = {
  module: "模块",
  class: "类",
  function: "函数",
  method: "方法",
};

function nodeMeta(node: MockNode): string {
  const source = node.filePath ?? node.path ?? "";
  const line = node.startLine ? `:${node.startLine}` : "";
  return `${source}${line}`;
}

function buildFlowNode(
  node: MockNode,
  childrenByParent: Map<string | null, MockNode[]>,
): ArchitectureFlowNode {
  const size = NODE_SIZES[node.kind];
  const children = childrenByParent.get(node.id) ?? [];
  return {
    id: node.id,
    type: "architecture",
    position: { x: 0, y: 0 },
    data: {
      label: node.label,
      kind: node.kind,
      kindLabel: KIND_LABELS[node.kind],
      meta: nodeMeta(node),
      summary: node.summary ?? "",
      childSummary: summarizeChildren(node, children),
      hasChildren: children.length > 0,
    },
    style: {
      width: size.width,
      height: size.height,
    },
  };
}

function buildFlowEdge(edge: MockEdge): Edge {
  const inferred = edge.confidence === "inferred";
  const color =
    edge.kind === "imports"
      ? inferred
        ? "#d6a33c"
        : "#4d6b91"
      : inferred
        ? "#d6a33c"
        : "#2b9d8b";

  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    type: "smoothstep",
    animated: inferred,
    markerEnd: {
      type: MarkerType.ArrowClosed,
      width: 16,
      height: 16,
      color,
    },
    style: {
      stroke: color,
      strokeWidth: inferred ? 1.8 : 1.5,
      strokeDasharray: inferred ? "6 5" : undefined,
    },
    label: inferred ? "推断" : undefined,
    labelStyle: {
      fill: "#e8b85b",
      fontSize: 10,
      fontWeight: 600,
    },
    labelBgStyle: {
      fill: "#0b1220",
      fillOpacity: 0.9,
    },
    labelBgPadding: [4, 2],
    labelBgBorderRadius: 4,
  };
}

export interface LayoutResult {
  nodes: ArchitectureFlowNode[];
  edges: Edge[];
}

/**
 * 用 Dagre 计算当前层级的自动布局。
 *
 * 当前层级通常只有模块/类/方法几十个节点，布局在渲染前同步完成；
 * 后续可以把更大层级的布局移到 Worker 或服务端。
 */
export function layoutGraph(
  mockNodes: MockNode[],
  mockEdges: MockEdge[],
  childrenByParent: Map<string | null, MockNode[]> = new Map(),
): LayoutResult {
  if (mockNodes.length === 0) {
    return { nodes: [], edges: [] };
  }

  // 小层级使用网格布局：项目根视图通常只有包容器，网格能让标签保持可读；
  // 大层级再交给 Dagre 做依赖方向布局。
  if (mockNodes.length <= 40) {
    const columns = Math.max(1, Math.ceil(Math.sqrt(mockNodes.length)));
    const cellWidth = 290;
    const cellHeight = 150;
    const nodes = mockNodes.map((mockNode, index) => {
      const flowNode = buildFlowNode(mockNode, childrenByParent);
      return {
        ...flowNode,
        position: {
          x: (index % columns) * cellWidth,
          y: Math.floor(index / columns) * cellHeight,
        },
      };
    });
    const visibleIds = new Set(mockNodes.map((node) => node.id));
    const edges = mockEdges
      .filter(
        (edge) =>
          visibleIds.has(edge.source) && visibleIds.has(edge.target),
      )
      .map(buildFlowEdge);
    return { nodes, edges };
  }

  const graph = new dagre.graphlib.Graph();
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setGraph({
    rankdir: "LR",
    align: "UL",
    nodesep: 32,
    ranksep: 58,
    marginx: 16,
    marginy: 16,
  });

  const visibleIds = new Set(mockNodes.map((node) => node.id));
  for (const mockNode of mockNodes) {
    const size = NODE_SIZES[mockNode.kind];
    graph.setNode(mockNode.id, { width: size.width, height: size.height });
  }
  for (const edge of mockEdges) {
    if (visibleIds.has(edge.source) && visibleIds.has(edge.target)) {
      graph.setEdge(edge.source, edge.target);
    }
  }

  dagre.layout(graph);

  const nodes = mockNodes.map((mockNode) => {
    const flowNode = buildFlowNode(mockNode, childrenByParent);
    const layoutNode = graph.node(mockNode.id);
    const size = NODE_SIZES[mockNode.kind];
    return {
      ...flowNode,
      position: {
        x: layoutNode.x - size.width / 2,
        y: layoutNode.y - size.height / 2,
      },
    };
  });

  const edges = mockEdges
    .filter(
      (edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target),
    )
    .map(buildFlowEdge);

  return { nodes, edges };
}
