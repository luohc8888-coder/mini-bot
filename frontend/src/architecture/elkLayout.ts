import dagre from "@dagrejs/dagre";
import type {
  ELK as ElkInstance,
  ElkEdgeSection,
  ElkExtendedEdge,
  ElkNode,
} from "elkjs/lib/elk-api";
import type {
  ArchitectureSourceRef,
  ArchitectureView,
} from "../types/architecture";
import {
  buildRuntimeFlowNode,
  buildRuntimeFlowNodes,
  runtimeNodeSize,
  type RuntimeEdgePoint,
  type RuntimeEdgeRoute,
  type RuntimeFlowNode,
} from "./layout";

/**
 * ELK Layered 配置：
 * - LEFT-TO-RIGHT 主方向；
 * - ORTHOGONAL 正交边；
 * - LAYER_SWEEP 交叉最小化；
 * - 节点/层级/边间距按真实节点尺寸计算。
 */
const ELK_LAYOUT_OPTIONS: Record<string, string> = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  "elk.edgeRouting": "ORTHOGONAL",
  "elk.layered.crossingMinimization.strategy": "LAYER_SWEEP",
  "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
  "elk.layered.cycleBreaking.strategy": "DEPTH_FIRST",
  "elk.layered.feedbackEdges": "true",
  // 长执行链在 10+ 节点时仍保持可读的宽高比；ELK 会把过长的层级链折叠成多行。
  "elk.layered.wrapping.strategy": "SINGLE_EDGE",
  "elk.aspectRatio": "1.6",
  "elk.spacing.nodeNode": "72",
  "elk.layered.spacing.nodeNodeBetweenLayers": "128",
  "elk.spacing.edgeNode": "36",
  "elk.spacing.edgeEdge": "22",
  "elk.layered.spacing.edgeNodeBetweenLayers": "44",
  "elk.padding": "[top=36,left=36,bottom=36,right=36]",
};

let elkInstance: ElkInstance | null = null;

async function getElk(): Promise<ElkInstance> {
  if (!elkInstance) {
    const { default: ElkConstructor } = await import("elkjs/lib/elk.bundled.js");
    elkInstance = new ElkConstructor();
  }
  return elkInstance;
}

export interface RuntimeLayoutOptions {
  mock: boolean;
  onNestedClick: (detailView: string) => void;
  onSourceOpen?: (source: ArchitectureSourceRef) => void;
}

export interface RuntimeLayoutResult {
  nodes: RuntimeFlowNode[];
  edgeRoutes: Record<string, RuntimeEdgeRoute>;
  usedElk: boolean;
}

function buildLegacyLayout(
  view: ArchitectureView,
  options: RuntimeLayoutOptions,
): RuntimeLayoutResult {
  return {
    nodes: buildRuntimeFlowNodes(
      view,
      options.mock,
      options.onNestedClick,
      options.onSourceOpen,
    ),
    edgeRoutes: {},
    usedElk: false,
  };
}

/**
 * ELK 不可用时的运行时 Dagre 回退。Dagre 负责节点位置；边仍由
 * RuntimeOrthogonalEdge 在没有 route points 时退回 React Flow smoothstep。
 */
function buildDagreFallback(
  view: ArchitectureView,
  options: RuntimeLayoutOptions,
): RuntimeLayoutResult {
  try {
    const graph = new dagre.graphlib.Graph();
    graph.setDefaultEdgeLabel(() => ({}));
    graph.setGraph({
      rankdir: "LR",
      align: "UL",
      nodesep: 42,
      ranksep: 86,
      marginx: 36,
      marginy: 36,
    });

    const nodeById = new Map(view.nodes.map((node) => [node.id, node]));
    for (const node of view.nodes) {
      const size = runtimeNodeSize(node);
      graph.setNode(node.id, { width: size.width, height: size.height });
    }
    for (const edge of view.edges) {
      if (nodeById.has(edge.source) && nodeById.has(edge.target)) {
        graph.setEdge(edge.source, edge.target);
      }
    }
    dagre.layout(graph);

    return {
      nodes: view.nodes.map((node) => {
        const size = runtimeNodeSize(node);
        const dagreNode = graph.node(node.id);
        const flowNode = buildRuntimeFlowNode(
          node,
          options.mock,
          options.onNestedClick,
          options.onSourceOpen,
        );
        return {
          ...flowNode,
          position: {
            x: (dagreNode?.x ?? size.width / 2) - size.width / 2,
            y: (dagreNode?.y ?? size.height / 2) - size.height / 2,
          },
        };
      }),
      edgeRoutes: {},
      usedElk: false,
    };
  } catch (error) {
    console.warn("Dagre architecture fallback failed; using grid", error);
    return buildLegacyLayout(view, options);
  }
}

function edgePoints(edge: ElkExtendedEdge | undefined): RuntimeEdgePoint[] {
  if (!edge?.sections?.length) {
    return [];
  }
  const points: RuntimeEdgePoint[] = [];
  for (const section of edge.sections) {
    appendSectionPoints(points, section);
  }
  return points;
}

function appendSectionPoints(
  points: RuntimeEdgePoint[],
  section: ElkEdgeSection,
): void {
  const sectionPoints: RuntimeEdgePoint[] = [
    section.startPoint,
    ...(section.bendPoints ?? []),
    section.endPoint,
  ];
  for (const point of sectionPoints) {
    const previous = points[points.length - 1];
    if (!previous || previous.x !== point.x || previous.y !== point.y) {
      points.push({ x: point.x, y: point.y });
    }
  }
}

function sourceHandleFor(
  point: RuntimeEdgePoint | undefined,
  node: ElkNode | undefined,
): string {
  if (!point || !node) {
    return "source-right";
  }
  const centerX = (node.x ?? 0) + (node.width ?? 0) / 2;
  return point.x >= centerX ? "source-right" : "source-left";
}

function targetHandleFor(
  point: RuntimeEdgePoint | undefined,
  node: ElkNode | undefined,
): string {
  if (!point || !node) {
    return "target-left";
  }
  const centerX = (node.x ?? 0) + (node.width ?? 0) / 2;
  return point.x >= centerX ? "target-right" : "target-left";
}

/**
 * 使用 ELK Layered 布局一个 ArchitectureView。
 *
 * 返回的 edgeRoutes 保存 ELK 计算出的 sections/bend points；React Flow
 * 的自定义正交 Edge 会直接使用这些点生成 SVG path，而不是重新走一遍
 * smoothstep。Mock 视图或已有手工 position 的视图保留旧布局。
 */
export async function layoutRuntimeView(
  view: ArchitectureView,
  options: RuntimeLayoutOptions,
): Promise<RuntimeLayoutResult> {
  if (
    options.mock ||
    view.nodes.length === 0 ||
    view.nodes.some((node) => node.position)
  ) {
    return buildLegacyLayout(view, options);
  }

  const nodeById = new Map(view.nodes.map((node) => [node.id, node]));
  const elkGraph: ElkNode = {
    id: "architecture-root",
    layoutOptions: ELK_LAYOUT_OPTIONS,
    children: view.nodes.map((node) => {
      const size = runtimeNodeSize(node);
      return {
        id: node.id,
        width: size.width,
        height: size.height,
      };
    }),
    edges: view.edges.map((edge) => ({
      id: edge.id,
      sources: [edge.source],
      targets: [edge.target],
    })),
  };

  let result: ElkNode;
  try {
    const elk = await getElk();
    result = (await elk.layout(elkGraph)) as ElkNode;
  } catch (error) {
    console.warn("ELK architecture layout failed; falling back to Dagre", error);
    return buildDagreFallback(view, options);
  }

  const laidOutById = new Map(
    (result.children ?? []).map((child) => [child.id, child]),
  );
  const laidOutEdges = new Map(
    (result.edges ?? []).map((edge) => [edge.id, edge]),
  );

  const nodes = view.nodes.map((node) => {
    const source = nodeById.get(node.id);
    const laidOut = laidOutById.get(node.id);
    const flowNode = buildRuntimeFlowNode(
      source ?? node,
      options.mock,
      options.onNestedClick,
      options.onSourceOpen,
    );
    return {
      ...flowNode,
      position: {
        x: laidOut?.x ?? 0,
        y: laidOut?.y ?? 0,
      },
    };
  });

  const edgeRoutes: Record<string, RuntimeEdgeRoute> = {};
  for (const edge of view.edges) {
    const elkEdge = laidOutEdges.get(edge.id) as ElkExtendedEdge | undefined;
    const points = edgePoints(elkEdge);
    const sourceNode = laidOutById.get(edge.source);
    const targetNode = laidOutById.get(edge.target);
    edgeRoutes[edge.id] = {
      points,
      sourceHandle: sourceHandleFor(points[0], sourceNode),
      targetHandle: targetHandleFor(points[points.length - 1], targetNode),
    };
  }

  return { nodes, edgeRoutes, usedElk: true };
}
