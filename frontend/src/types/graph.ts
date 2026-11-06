import type { ArchitectureNodeKind } from "./architecture";

export type NodeKind = "module" | "class" | "function" | "method";

export type EdgeKind = "contains" | "imports" | "calls";

export type Confidence = "static" | "inferred";

export interface MockNode {
  id: string;
  kind: NodeKind;
  label: string;
  parentId: string | null;
  /** Logical source directory or module path used for display. */
  path?: string;
  /** Workspace-relative Python file path. */
  filePath?: string;
  startLine?: number;
  endLine?: number;
  signature?: string;
  summary?: string;
  sourcePreview?: string[];
  /** 用于前端包视图分组的模块全名。 */
  qualifiedName?: string;
  /** 前端生成的包视图节点，不是 CodeGraph 原始节点。 */
  synthetic?: boolean;
}

export interface MockEdge {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  confidence: Confidence;
  note?: string;
}

export interface MockProject {
  name: string;
  root: string;
  description: string;
  version: string;
}

export interface MockGraph {
  project: MockProject;
  nodes: MockNode[];
  edges: MockEdge[];
}

/** FastAPI `/api/project` 返回结构。 */
export interface ApiProject {
  name: string;
  root: string;
  workspaceId: string;
  description?: string;
  version?: string;
  generatedAt?: string;
  stats: Record<string, number>;
  graphStats: Record<string, number>;
}

/** FastAPI `/api/graph` 返回的节点。 */
export interface ApiNode {
  id: string;
  kind: NodeKind;
  name: string;
  qualifiedName?: string;
  parentId?: string | null;
  filePath?: string;
  startLine?: number;
  endLine?: number;
  signature?: string | null;
  docstring?: string | null;
  language?: string;
}

/** FastAPI `/api/graph` 返回的边。 */
export interface ApiEdge {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  confidence: Confidence;
  line?: number | null;
  detail?: string | null;
  occurrences?: number;
}

export interface ApiGraph {
  schemaVersion: number;
  workspaceRoot: string;
  workspaceId: string;
  nodes: ApiNode[];
  edges: ApiEdge[];
  stats: Record<string, number>;
  unresolved?: unknown[];
}

export interface BreadcrumbItem {
  id: string | null;
  label: string;
  kind: "project" | NodeKind | ArchitectureNodeKind;
  isCurrent: boolean;
  clickable: boolean;
}

export interface ViewModel {
  containerId: string | null;
  selectedId: string | null;
  visibleNodes: MockNode[];
  visibleEdges: MockEdge[];
  breadcrumbs: BreadcrumbItem[];
  inspectorNode: MockNode | null;
}

export interface ViewStats {
  modules: number;
  classes: number;
  functions: number;
  methods: number;
}
