import type { ArchitectureGraph } from "../types/architecture";

export interface ArchitectureStatus {
  configured: boolean;
  profile: string | null;
  provider: string;
  model: string;
  available: boolean;
  generatedAt: string | null;
  cachePath: string;
  error: string | null;
}

export interface ArchitectureAnalysisResponse {
  graph: ArchitectureGraph;
  cached: boolean;
  provider: string;
  model: string;
  promptTokens: number;
  completionTokens: number;
  totalTokens: number;
  attempts: number;
  promptTrimmed: boolean;
  warnings: string[];
  durationMs: number;
  promptStats: Record<string, unknown>;
}

export class ArchitectureApiError extends Error {
  readonly status: number;
  readonly issues: string[];

  constructor(message: string, status: number, issues: string[] = []) {
    super(message);
    this.name = "ArchitectureApiError";
    this.status = status;
    this.issues = issues;
  }
}

async function readError(response: Response): Promise<ArchitectureApiError> {
  let message = `请求失败（HTTP ${response.status}）`;
  let issues: string[] = [];
  try {
    const payload = (await response.json()) as {
      detail?: unknown;
      issues?: unknown;
    };
    const detail = payload?.detail;
    if (typeof detail === "string") {
      message = detail;
    } else if (detail && typeof detail === "object") {
      const nested = detail as { detail?: unknown; issues?: unknown };
      if (typeof nested.detail === "string") {
        message = nested.detail;
      }
      if (Array.isArray(nested.issues)) {
        issues = nested.issues.filter((item): item is string => typeof item === "string");
      }
    }
    if (issues.length === 0 && Array.isArray(payload?.issues)) {
      issues = payload.issues.filter((item): item is string => typeof item === "string");
    }
  } catch {
    // 非 JSON 错误响应保留 HTTP 状态信息。
  }
  return new ArchitectureApiError(message, response.status, issues);
}

async function requestJson<T>(input: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(input, init);
  } catch (error) {
    throw new ArchitectureApiError(
      `无法连接本地 Web 服务：${error instanceof Error ? error.message : String(error)}`,
      0,
    );
  }
  if (!response.ok) {
    throw await readError(response);
  }
  return (await response.json()) as T;
}

export function fetchArchitectureStatus(): Promise<ArchitectureStatus> {
  return requestJson<ArchitectureStatus>("/api/architecture/status");
}

export function fetchArchitectureGraph(): Promise<ArchitectureGraph> {
  return requestJson<ArchitectureGraph>("/api/architecture");
}

export async function generateArchitecture(
  force = false,
): Promise<ArchitectureAnalysisResponse> {
  const query = force ? "?force=true" : "";
  return requestJson<ArchitectureAnalysisResponse>(
    `/api/architecture/analyze${query}`,
    { method: "POST" },
  );
}

export function architectureErrorMessage(error: unknown): string {
  if (error instanceof ArchitectureApiError) {
    if (error.issues.length > 0) {
      return `${error.message}：${error.issues.slice(0, 3).join("；")}`;
    }
    return error.message;
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}
