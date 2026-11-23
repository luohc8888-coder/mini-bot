export type ArchitectureNodeKind = "component" | "stage" | "condition";

export type ArchitectureEdgeKind = "flow" | "data" | "branch" | "dependency";

export type ArchitectureConfidence = "static" | "inferred";

/** 逻辑组件/阶段关联到真实源码的范围。Phase 4.2 起由后端 SymbolIndex 校验。 */
export interface ArchitectureSourceRef {
  symbolId?: string;
  filePath?: string;
  startLine?: number;
  endLine?: number;
  qualifiedName?: string;
  /** 后端 SymbolIndex 校验成功后为 true；Mock 阶段保持 false。 */
  verified?: boolean;
  symbolName?: string;
  note?: string;
}

/** 后端按 symbolId 读取的只读源码片段。 */
export interface SourceSnippetLine {
  number: number;
  text: string;
}

export interface SourceSnippet {
  symbolId: string;
  symbolName: string;
  kind: string;
  qualifiedName: string;
  filePath: string;
  language: string;
  signature?: string | null;
  docstring?: string | null;
  symbolStartLine: number;
  symbolEndLine: number;
  startLine: number;
  endLine: number;
  totalLines: number;
  fullFile?: boolean;
  truncated: boolean;
  lines: SourceSnippetLine[];
}

/** 在父组件卡片内部展示的嵌套组件入口。 */
export interface ArchitectureNestedNode {
  id: string;
  label: string;
  kind: ArchitectureNodeKind;
  detailView?: string;
}

export interface ArchitectureNode {
  id: string;
  kind: ArchitectureNodeKind;
  label: string;
  description: string;
  meta?: string;
  parentId?: string;
  confidence?: ArchitectureConfidence;
  evidence?: string[];
  position?: { x: number; y: number };
  size?: { width: number; height: number };
  /** 点击该节点时进入的局部执行流程视图。 */
  detailView?: string;
  responsibilities?: string[];
  sourceRefs?: ArchitectureSourceRef[];
  /** 仅作为当前组件的视觉嵌套预览，不参与 CodeGraph 统计。 */
  nested?: ArchitectureNestedNode[];
}

export interface ArchitectureEdge {
  id: string;
  source: string;
  target: string;
  kind: ArchitectureEdgeKind;
  confidence: ArchitectureConfidence;
  label?: string;
  evidence?: string[];
  sourceRefs?: ArchitectureSourceRef[];
  note?: string;
}

export interface ArchitectureView {
  id: string;
  title: string;
  subtitle: string;
  nodes: ArchitectureNode[];
  edges: ArchitectureEdge[];
}

export interface ArchitectureGraph {
  schemaVersion?: number;
  version: string;
  mock?: boolean;
  project: {
    name: string;
    root: string;
    description: string;
  };
  defaultViewId: string;
  views: Record<string, ArchitectureView>;
}

export type ExplorerViewMode = "architecture" | "structure";

export interface GitHeadInfo {
  hash: string;
  subject: string;
  relativeTime: string;
}

export interface GitChangedFile {
  path: string;
  status: string;
  category: "modified" | "added" | "deleted" | "renamed" | "untracked";
  staged: boolean;
  unstaged: boolean;
  symbolId: string | null;
  symbolName: string | null;
  symbolCount: number;
}

export interface GitStatusSummary {
  total: number;
  modified: number;
  added: number;
  deleted: number;
  renamed: number;
  untracked: number;
}

export interface GitStatusResponse {
  available: boolean;
  reason: string | null;
  branch: string | null;
  head: GitHeadInfo | null;
  files: GitChangedFile[];
  truncated: boolean;
  summary: GitStatusSummary;
}

export interface GitDiffLine {
  kind: "meta" | "hunk" | "add" | "del" | "context";
  oldLine: number | null;
  newLine: number | null;
  text: string;
}

export interface GitFileSymbol {
  symbolId: string;
  name: string;
  kind: string;
  qualifiedName: string;
  startLine: number;
  endLine: number;
}

export interface GitDiffResponse {
  filePath: string;
  status: string;
  category: string;
  additions: number;
  deletions: number;
  isBinary: boolean;
  truncated: boolean;
  lines: GitDiffLine[];
  symbols: GitFileSymbol[];
}
