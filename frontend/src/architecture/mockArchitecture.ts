import rawGraph from "../mocks/architecture-graph.json";
import type { ArchitectureGraph } from "../types/architecture";

/**
 * Phase 4.1 的 ArchitectureGraph 原型数据，仅保留给设计回归与测试使用。
 * Phase 4.3 的运行架构页面只读取后端 API；不会把 mock 数据当成 LLM 结果。
 */
export const mockArchitecture = rawGraph as unknown as ArchitectureGraph;

export {
  architectureViewStats,
  findArchitectureNode,
  getArchitectureView,
} from "./graph";
