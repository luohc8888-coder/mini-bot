import { MarkerType, type Edge, type Node } from "@xyflow/react";
import type {
  ArchitectureEdge,
  ArchitectureGraph,
  ArchitectureNode,
  ArchitectureNodeKind,
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
  mock: boolean;
  onNestedClick?: (detailView: string) => void;
}

export type RuntimeFlowNode = Node<RuntimeNodeData, "runtime">;

const KIND_LABELS: Record<ArchitectureNodeKind, string> = {
  component: "逻辑组件",
  stage: "执行阶段",
  condition: "判断条件",
};

const DEFAULT_SIZES: Record<ArchitectureNodeKind, { width: number; height: number }> = {
  component: { width: 360, height: 232 },
  stage: { width: 220, height: 128 },
  condition: { width: 190, height: 118 },
};

const EDGE_COLORS: Record<ArchitectureEdge["kind"], string> = {
  flow: "#6f9cff",
  data: "#35d4a4",
  branch: "#f2c94c",
  dependency: "#b88cff",
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

export function buildRuntimeFlowNodes(
  view: ArchitectureView,
  mock: boolean,
  onNestedClick: (detailView: string) => void,
): RuntimeFlowNode[] {
  return view.nodes.map((node, index) => {
    const size = runtimeNodeSize(node);
    return {
      id: node.id,
      type: "runtime",
      position: node.position ?? {
        x: (index % 4) * 320,
        y: Math.floor(index / 4) * 180,
      },
      data: {
        label: node.label,
        kind: node.kind,
        kindLabel: KIND_LABELS[node.kind],
        description: node.description,
        meta: node.meta ?? "",
        detailView: node.detailView,
        nested: node.nested ?? [],
        mock,
        onNestedClick,
      },
      style: {
        width: size.width,
        height: size.height,
      },
    };
  });
}

export function buildRuntimeFlowEdge(edge: ArchitectureEdge): Edge {
  const inferred = edge.confidence === "inferred";
  const color = EDGE_COLORS[edge.kind];
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
      strokeWidth: edge.kind === "flow" ? 1.8 : 1.5,
      strokeDasharray: inferred ? "6 5" : undefined,
    },
    label: edge.label,
    labelStyle: {
      fill: color,
      fontSize: 10,
      fontWeight: 600,
    },
    labelBgStyle: {
      fill: "#0b1220",
      fillOpacity: 0.92,
    },
    labelBgPadding: [4, 2],
    labelBgBorderRadius: 4,
  };
}

export function runtimeGraphSummary(graph: ArchitectureGraph, view: ArchitectureView): string {
  const componentCount = view.nodes.filter((node) => node.kind === "component").length;
  const conditionCount = view.nodes.filter((node) => node.kind === "condition").length;
  return `${componentCount} 个组件 · ${conditionCount} 个条件 · ${graph.mock ? "Mock 数据" : "已校验数据"}`;
}
