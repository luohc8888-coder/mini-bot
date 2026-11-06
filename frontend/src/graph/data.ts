import type {
  ApiEdge,
  ApiGraph,
  ApiNode,
  ApiProject,
  BreadcrumbItem,
  MockEdge,
  MockNode,
  MockProject,
  ViewModel,
  ViewStats,
} from "../types/graph";

export interface GraphData {
  project: MockProject;
  nodes: MockNode[];
  edges: MockEdge[];
  stats: ViewStats;
}

export interface GraphIndex {
  data: GraphData;
  nodeById: Map<string, MockNode>;
  childrenByParent: Map<string | null, MockNode[]>;
}

function numberOrZero(value: unknown): number {
  return typeof value === "number" ? value : 0;
}

function buildStats(stats: Record<string, number>): ViewStats {
  return {
    modules: numberOrZero(stats.modules),
    classes: numberOrZero(stats.classes),
    functions: numberOrZero(stats.functions),
    methods: numberOrZero(stats.methods),
  };
}

function toMockEdge(edge: ApiEdge): MockEdge {
  return {
    id: edge.id,
    source: edge.source,
    target: edge.target,
    kind: edge.kind,
    confidence: edge.confidence,
    note: edge.detail ?? undefined,
  };
}

function toMockNode(node: ApiNode): MockNode {
  return {
    id: node.id,
    kind: node.kind,
    label: node.name,
    parentId: node.parentId ?? null,
    path: node.filePath,
    filePath: node.filePath,
    startLine: node.startLine,
    endLine: node.endLine,
    signature: node.signature ?? undefined,
    summary: node.docstring ?? undefined,
    qualifiedName: node.qualifiedName,
  };
}

function moduleDirectory(filePath: string | undefined): string | null {
  if (!filePath) {
    return null;
  }
  const parts = filePath.split("/");
  if (parts.length <= 1) {
    return null;
  }
  return parts.slice(0, -1).join("/");
}

function rootContainerId(
  nodeId: string,
  nodeById: Map<string, MockNode>,
): string {
  let current = nodeById.get(nodeId);
  const seen = new Set<string>();
  while (current && current.parentId && !seen.has(current.id)) {
    seen.add(current.id);
    const parent = nodeById.get(current.parentId);
    if (!parent) {
      break;
    }
    current = parent;
  }
  return current?.id ?? nodeId;
}

/**
 * 生成前端包视图：目录直接作为一层模块容器，避免真实项目在根视图一次
 * 铺开上百个文件模块。包节点是纯展示节点，不参与原始 CodeGraph 统计。
 */
function buildPackageViewNodes(nodes: MockNode[]): MockNode[] {
  const modulesByDirectory = new Map<string, MockNode[]>();
  for (const node of nodes) {
    if (node.kind !== "module" || !node.filePath) {
      continue;
    }
    const directory = moduleDirectory(node.filePath);
    if (!directory) {
      continue;
    }
    const siblings = modulesByDirectory.get(directory) ?? [];
    siblings.push(node);
    modulesByDirectory.set(directory, siblings);
  }

  const packageNodes: MockNode[] = [];
  for (const [directory, modules] of modulesByDirectory) {
    const id = `package:${directory}`;
    const label = directory.split("/").pop() ?? directory;
    packageNodes.push({
      id,
      kind: "module",
      label,
      parentId: null,
      path: directory,
      summary: `${modules.length} 个模块`,
      synthetic: true,
    });
    for (const module of modules) {
      module.parentId = id;
    }
  }
  return packageNodes;
}

function buildPackageViewEdges(
  nodes: MockNode[],
  edges: MockEdge[],
): MockEdge[] {
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const aggregated = new Map<
    string,
    { source: string; target: string; kind: MockEdge["kind"]; count: number }
  >();

  for (const edge of edges) {
    if (edge.kind === "contains") {
      continue;
    }
    const source = rootContainerId(edge.source, nodeById);
    const target = rootContainerId(edge.target, nodeById);
    if (source === target || (!source.startsWith("package:") && !target.startsWith("package:"))) {
      continue;
    }
    const key = `${edge.kind}:${source}:${target}`;
    const current = aggregated.get(key);
    if (current) {
      current.count += 1;
    } else {
      aggregated.set(key, {
        source,
        target,
        kind: edge.kind,
        count: 1,
      });
    }
  }

  return [...aggregated.entries()].map(([key, item]) => ({
    id: `package-view:${key}`,
    source: item.source,
    target: item.target,
    kind: item.kind,
    confidence: "static",
    note: `${item.count} 条关系`,
  }));
}

/** 把 API 的 snake/qualified 字段映射成 React Flow 面板使用的节点模型。 */
export function buildGraphData(
  project: ApiProject,
  graph: ApiGraph,
): GraphData {
  const nodes = graph.nodes.map(toMockNode);
  const packageNodes = buildPackageViewNodes(nodes);
  const edges = [...graph.edges.map(toMockEdge)];
  edges.push(...buildPackageViewEdges([...packageNodes, ...nodes], edges));

  return {
    project: {
      name: project.name,
      root: project.root,
      description:
        project.description ?? `${project.name} 的本地静态代码结构`,
      version: project.version ?? "V3.0",
    },
    nodes: [...packageNodes, ...nodes],
    edges,
    stats: buildStats(graph.stats),
  };
}

export function buildGraphIndex(data: GraphData): GraphIndex {
  const nodeById = new Map<string, MockNode>(
    data.nodes.map((node) => [node.id, node]),
  );
  const childrenByParent = new Map<string | null, MockNode[]>();
  for (const node of data.nodes) {
    const siblings = childrenByParent.get(node.parentId) ?? [];
    siblings.push(node);
    childrenByParent.set(node.parentId, siblings);
  }
  return { data, nodeById, childrenByParent };
}

export function getNode(
  index: GraphIndex,
  nodeId: string | null,
): MockNode | null {
  return nodeId ? index.nodeById.get(nodeId) ?? null : null;
}

export function getChildren(
  index: GraphIndex,
  parentId: string | null,
): MockNode[] {
  return index.childrenByParent.get(parentId) ?? [];
}

export function hasChildren(index: GraphIndex, nodeId: string): boolean {
  return getChildren(index, nodeId).length > 0;
}

export function getPathToRoot(
  index: GraphIndex,
  nodeId: string | null,
): MockNode[] {
  const path: MockNode[] = [];
  let current = getNode(index, nodeId);
  while (current) {
    path.push(current);
    current = getNode(index, current.parentId);
  }
  return path.reverse();
}

export function buildBreadcrumbs(
  index: GraphIndex,
  containerId: string | null,
  selectedId: string | null,
): BreadcrumbItem[] {
  const items: BreadcrumbItem[] = [
    {
      id: null,
      label: index.data.project.name,
      kind: "project",
      isCurrent: containerId === null && selectedId === null,
      clickable: containerId !== null || selectedId !== null,
    },
  ];

  if (containerId) {
    const path = getPathToRoot(index, containerId);
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

  const selected = getNode(index, selectedId);
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

export function summarizeChildren(node: MockNode, children: MockNode[]): string {
  if (children.length === 0) {
    return "";
  }
  const classes = children.filter((child) => child.kind === "class").length;
  const functions = children.filter((child) => child.kind === "function").length;
  const methods = children.filter((child) => child.kind === "method").length;

  if (node.kind === "module") {
    const modules = children.filter((child) => child.kind === "module").length;
    if (classes === 0 && functions === 0 && modules > 0) {
      return `${modules} 个模块`;
    }
    return `${classes} 个类 · ${functions} 个函数`;
  }
  if (node.kind === "class") {
    return `${methods} 个方法`;
  }
  return `${children.length} 个子节点`;
}

export function getChildSummary(index: GraphIndex, node: MockNode): string {
  return summarizeChildren(node, getChildren(index, node.id));
}

export function buildViewModel(
  index: GraphIndex,
  containerId: string | null,
  selectedId: string | null,
): ViewModel {
  const visibleNodes = getChildren(index, containerId);
  const visibleIds = new Set(visibleNodes.map((node) => node.id));
  const visibleEdges = index.data.edges.filter(
    (edge) =>
      edge.kind !== "contains" &&
      visibleIds.has(edge.source) &&
      visibleIds.has(edge.target),
  );

  return {
    containerId,
    selectedId,
    visibleNodes,
    visibleEdges,
    breadcrumbs: buildBreadcrumbs(index, containerId, selectedId),
    inspectorNode: getNode(index, selectedId) ?? getNode(index, containerId),
  };
}
