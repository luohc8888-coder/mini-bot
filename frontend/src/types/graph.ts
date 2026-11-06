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

export interface BreadcrumbItem {
  id: string | null;
  label: string;
  kind: "project" | NodeKind;
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
