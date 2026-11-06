import rawGraph from "../mocks/architecture-graph.json";
import type { ArchitectureGraph, ArchitectureNode } from "../types/architecture";

/**
 * Phase 4.1 的 ArchitectureGraph 原型数据。
 *
 * 数据刻意保持 mock=true：本阶段不调用 LLM，也不读取后端 API。
 * Phase 4.2/4.3 会用同构的 JSON 契约替换为经过校验的真实分析结果。
 */
export const mockArchitecture = rawGraph as unknown as ArchitectureGraph;

export function getArchitectureView(
  graph: ArchitectureGraph,
  viewId: string,
): ArchitectureGraph["views"][string] {
  return graph.views[viewId] ?? graph.views[graph.defaultViewId];
}

export function findArchitectureNode(
  graph: ArchitectureGraph,
  viewId: string,
  nodeId: string | null,
): ArchitectureNode | null {
  if (!nodeId) {
    return null;
  }
  return (
    getArchitectureView(graph, viewId).nodes.find((node) => node.id === nodeId) ??
    null
  );
}

export function architectureViewStats(
  graph: ArchitectureGraph,
  viewId: string,
): { nodes: number; edges: number; components: number; conditions: number } {
  const view = getArchitectureView(graph, viewId);
  return {
    nodes: view.nodes.length,
    edges: view.edges.length,
    components: view.nodes.filter((node) => node.kind === "component").length,
    conditions: view.nodes.filter((node) => node.kind === "condition").length,
  };
}
