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
