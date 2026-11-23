import type {
  ArchitectureGraph,
  ArchitectureSourceRef,
  GitDiffResponse,
  GitStatusResponse,
  SourceSnippet,
} from "../types/architecture";

export interface ArchitectureStatus {
  configured: boolean;
  profile: string | null;
  provider: string;
  model: string;
  available: boolean;
  cacheStatus?: "valid" | "stale" | "missing";
  generatedAt: string | null;
  cacheGeneratedAt?: string | null;
  cacheStoredFingerprint?: string | null;
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

export interface ChatReply {
  message: string;
  provider: string;
  model: string;
  usage?: Record<string, unknown>;
}

export interface ChatHistoryItem {
  role: "user" | "assistant";
  content: string;
}

export interface ChatRequest {
  message: string;
  symbolId?: string;
  nodeLabel?: string;
  history?: ChatHistoryItem[];
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
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
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

export function fetchArchitectureGraph(
  allowStale = false,
): Promise<ArchitectureGraph> {
  const query = allowStale ? "?allow_stale=true" : "";
  return requestJson<ArchitectureGraph>(`/api/architecture${query}`);
}

export function fetchSourceSnippet(
  symbolId: string,
  signal?: AbortSignal,
  limit = 10000,
  fullFile = false,
): Promise<SourceSnippet> {
  const query = new URLSearchParams({
    symbol_id: symbolId,
    // 源码工作台展示完整符号范围或完整文件，不再被默认的 400 行窗口截断。
    limit: String(limit),
  });
  if (fullFile) {
    query.set("full_file", "true");
  }
  return requestJson<SourceSnippet>(`/api/source?${query.toString()}`, {
    signal,
  });
}

export function fetchChatReply(
  request: ChatRequest,
  signal?: AbortSignal,
): Promise<ChatReply> {
  return requestJson<ChatReply>("/api/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(request),
    signal,
  });
}

export interface StreamChatDoneEvent {
  message: string;
  toolsUsed: string[];
  stopReason: string;
  streamed: boolean;
  provider: string;
  model: string;
  ttftMs: number;
  durationMs: number;
}

export interface StreamChatCallbacks {
  onDelta: (text: string) => void;
  onTool: (toolName: string) => void;
  onDone: (payload: StreamChatDoneEvent) => void;
}

export async function streamChatReply(
  request: ChatRequest,
  callbacks: StreamChatCallbacks,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify(request),
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new ArchitectureApiError(
      `无法连接本地 Web 服务：${error instanceof Error ? error.message : String(error)}`,
      0,
    );
  }

  // 兼容旧版未提供流式接口的后端，自动回退到 /api/chat
  if (response.status === 404 || response.status === 405) {
    const reply = await fetchChatReply(request, signal);
    callbacks.onDone({
      message: reply.message,
      toolsUsed: [],
      stopReason: "completed",
      streamed: false,
      provider: reply.provider,
      model: reply.model,
      ttftMs: 0,
      durationMs: 0,
    });
    return;
  }

  if (!response.ok) {
    throw await readError(response);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new ArchitectureApiError("浏览器不支持流式响应读取。", 0);
  }

  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let completed = false;

  const dispatchFrame = (rawFrame: string) => {
    const lines = rawFrame.split(/\r?\n/);
    let eventName = "message";
    const dataLines: string[] = [];
    for (const line of lines) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    if (dataLines.length === 0) {
      return;
    }
    const rawData = dataLines.join("\n");
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(rawData) as Record<string, unknown>;
    } catch {
      return;
    }
    if (eventName === "delta" && typeof parsed.text === "string") {
      callbacks.onDelta(parsed.text);
    } else if (eventName === "tool" && typeof parsed.name === "string") {
      callbacks.onTool(parsed.name);
    } else if (eventName === "error") {
      throw new ArchitectureApiError(
        typeof parsed.detail === "string" ? parsed.detail : "流式对话失败。",
        502,
      );
    } else if (eventName === "done") {
      completed = true;
      callbacks.onDone({
        message: typeof parsed.message === "string" ? parsed.message : "",
        toolsUsed: Array.isArray(parsed.toolsUsed)
          ? parsed.toolsUsed.filter((item): item is string => typeof item === "string")
          : [],
        stopReason: typeof parsed.stopReason === "string" ? parsed.stopReason : "completed",
        streamed: Boolean(parsed.streamed),
        provider: typeof parsed.provider === "string" ? parsed.provider : "",
        model: typeof parsed.model === "string" ? parsed.model : "",
        ttftMs: typeof parsed.ttftMs === "number" ? parsed.ttftMs : 0,
        durationMs: typeof parsed.durationMs === "number" ? parsed.durationMs : 0,
      });
    }
  };

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let sepIndex = buffer.indexOf("\n\n");
      while (sepIndex >= 0) {
        const frame = buffer.slice(0, sepIndex).trim();
        buffer = buffer.slice(sepIndex + 2);
        if (frame) {
          dispatchFrame(frame);
        }
        sepIndex = buffer.indexOf("\n\n");
      }
    }
    const trailing = (buffer + decoder.decode()).trim();
    if (trailing) {
      dispatchFrame(trailing);
    }
  } finally {
    reader.releaseLock();
  }

  if (!completed && !signal?.aborted) {
    throw new ArchitectureApiError("流式连接提前中断，未收到完整结束信号。", 0);
  }
}

export function resolveSymbolByPath(
  filePath: string,
  line?: number,
  signal?: AbortSignal,
): Promise<ArchitectureSourceRef> {
  const query = new URLSearchParams({ file_path: filePath });
  if (line && line >= 1) {
    query.set("line", String(line));
  }
  return requestJson<ArchitectureSourceRef>(
    `/api/symbol-by-path?${query.toString()}`,
    { signal },
  );
}

export function fetchGitStatus(
  signal?: AbortSignal,
): Promise<GitStatusResponse> {
  return requestJson<GitStatusResponse>("/api/git/status", { signal });
}

export function fetchGitDiff(
  filePath: string,
  signal?: AbortSignal,
): Promise<GitDiffResponse> {
  const query = new URLSearchParams({ file: filePath });
  return requestJson<GitDiffResponse>(`/api/git/diff?${query.toString()}`, {
    signal,
  });
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
    if (error.status === 405) {
      return "当前后端尚未加载 AI 对话接口，请重启 mini-bot web 后重试。";
    }
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
