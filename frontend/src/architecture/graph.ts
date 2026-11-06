import type { ArchitectureGraph, ArchitectureNode } from "../types/architecture";

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
