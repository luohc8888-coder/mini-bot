import type { ApiGraph, ApiProject } from "../types/graph";

export interface ExplorerData {
  project: ApiProject;
  graph: ApiGraph;
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, {
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail ?? "";
    } catch {
      // 非 JSON 错误体不阻断错误提示。
    }
    throw new Error(
      detail
        ? `${url} 返回 HTTP ${response.status}：${detail}`
        : `${url} 返回 HTTP ${response.status}`,
    );
  }
  return (await response.json()) as T;
}

/** 从当前 FastAPI 服务加载项目元信息和完整 CodeGraph。 */
export async function fetchExplorerData(): Promise<ExplorerData> {
  const [project, graph] = await Promise.all([
    getJson<ApiProject>("/api/project"),
    getJson<ApiGraph>("/api/graph"),
  ]);
  return { project, graph };
}
