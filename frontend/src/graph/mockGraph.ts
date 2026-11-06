import rawGraph from "../mocks/project-graph.json";
import type {
  BreadcrumbItem,
  MockEdge,
  MockGraph,
  MockNode,
  ViewModel,
  ViewStats,
} from "../types/graph";

export const mockGraph = rawGraph as unknown as MockGraph;

const nodeById = new Map<string, MockNode>(
  mockGraph.nodes.map((node) => [node.id, node]),
);

const childrenByParent = new Map<string | null, MockNode[]>();
for (const node of mockGraph.nodes) {
  const siblings = childrenByParent.get(node.parentId) ?? [];
  siblings.push(node);
  childrenByParent.set(node.parentId, siblings);
}

export function getNode(nodeId: string | null): MockNode | null {
  return nodeId ? nodeById.get(nodeId) ?? null : null;
}

export function getChildren(parentId: string | null): MockNode[] {
  return childrenByParent.get(parentId) ?? [];
}

export function hasChildren(nodeId: string): boolean {
  return getChildren(nodeId).length > 0;
}

export function getPathToRoot(nodeId: string): MockNode[] {
  const path: MockNode[] = [];
  let current = getNode(nodeId);
  while (current) {
    path.push(current);
    current = getNode(current.parentId);
  }
  return path.reverse();
}

export function buildBreadcrumbs(
  containerId: string | null,
  selectedId: string | null,
): BreadcrumbItem[] {
  const items: BreadcrumbItem[] = [
    {
      id: null,
      label: mockGraph.project.name,
      kind: "project",
      isCurrent: containerId === null && selectedId === null,
      clickable: containerId !== null || selectedId !== null,
    },
  ];

  if (containerId) {
    const path = getPathToRoot(containerId);
    for (const node of path) {
      items.push({
        id: node.id,
        label: node.label,
        kind: node.kind,
        isCurrent: node.id === containerId && selectedId === null,
        clickable: node.id !== containerId || selectedId !== null,
      });
    }
  }

  const selected = getNode(selectedId);
  if (selected && selected.id !== containerId) {
    items.push({
      id: selected.id,
      label: selected.label,
      kind: selected.kind,
      isCurrent: true,
      clickable: false,
    });
  }

  return items;
}

export function buildViewModel(
  containerId: string | null,
  selectedId: string | null,
): ViewModel {
  const visibleNodes = getChildren(containerId);
  const visibleIds = new Set(visibleNodes.map((node) => node.id));
  const visibleEdges = mockGraph.edges.filter(
    (edge) => visibleIds.has(edge.source) && visibleIds.has(edge.target),
  );

  return {
    containerId,
    selectedId,
    visibleNodes,
    visibleEdges,
    breadcrumbs: buildBreadcrumbs(containerId, selectedId),
    inspectorNode: getNode(selectedId) ?? getNode(containerId),
  };
}

export function getProjectStats(): ViewStats {
  const stats: ViewStats = {
    modules: 0,
    classes: 0,
    functions: 0,
    methods: 0,
  };
  for (const node of mockGraph.nodes) {
    if (node.kind === "module") stats.modules += 1;
    if (node.kind === "class") stats.classes += 1;
    if (node.kind === "function") stats.functions += 1;
    if (node.kind === "method") stats.methods += 1;
  }
  return stats;
}

export function getChildSummary(node: MockNode): string {
  const children = getChildren(node.id);
  if (children.length === 0) {
    return "";
  }
  const classes = children.filter((child) => child.kind === "class").length;
  const functions = children.filter((child) => child.kind === "function").length;
  const methods = children.filter((child) => child.kind === "method").length;

  if (node.kind === "module") {
    return `${classes} 个类 · ${functions} 个函数`;
  }
  if (node.kind === "class") {
    return `${methods} 个方法`;
  }
  return `${children.length} 个子节点`;
}

export function getVisibleEdgeCount(
  visibleNodes: MockNode[],
  visibleEdges: MockEdge[],
): number {
  if (visibleNodes.length === 0) {
    return 0;
  }
  return visibleEdges.length;
}
