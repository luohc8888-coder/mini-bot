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
  /** Phase 4.2 起由后端返回；Mock 数据只有 version。 */
  schemaVersion?: number;
  version: string;
  mock: boolean;
  project: {
    name: string;
    root: string;
    description: string;
  };
  defaultViewId: string;
  views: Record<string, ArchitectureView>;
}

export type ExplorerViewMode = "architecture" | "structure";
