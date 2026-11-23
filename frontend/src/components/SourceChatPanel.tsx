import { Fragment, useEffect, useRef, useState, type ReactNode } from "react";
import {
  architectureErrorMessage,
  streamChatReply,
  type ChatHistoryItem,
} from "../architecture/api";
import type { ArchitectureSourceRef } from "../types/architecture";

interface SourceChatPanelProps {
  source: ArchitectureSourceRef;
  nodeLabel?: string;
  configured: boolean;
  onJumpToLine?: (startLine: number, endLine: number) => void;
  onJumpToFileLine?: (
    filePath: string,
    startLine: number,
    endLine: number,
  ) => void;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  pending?: boolean;
  error?: boolean;
  welcome?: boolean;
  toolsUsed?: string[];
  ttftMs?: number;
  durationMs?: number;
}

interface AnchorCallbacks {
  onJumpToLine?: (startLine: number, endLine: number) => void;
  onJumpToFileLine?: (
    filePath: string,
    startLine: number,
    endLine: number,
  ) => void;
}

const QUICK_PROMPTS = [
  "总结核心职责与关键行号",
  "追踪上下游调用链（callers / callees）",
  "分析潜在边界问题或风险点",
];

// 匹配行内代码、加粗文本、跨文件路径:行号 与 当前文件 L行号 锚点
const INLINE_TOKEN_RE =
  /(`[^`]+`|\*\*[^*]+\*\*|(?:[A-Za-z0-9_.-]+\/)+[A-Za-z0-9_.-]+\.(?:py|tsx?|jsx?|mjs|cjs|java):[0-9]+(?:-[0-9]+)?|\bL[0-9]+(?:-L?[0-9]+)?\b)/g;
const FILE_LINE_RE =
  /^((?:[A-Za-z0-9_.-]+\/)+[A-Za-z0-9_.-]+\.(?:py|tsx?|jsx?|mjs|cjs|java)):([0-9]+)(?:-([0-9]+))?$/;
const LINE_ONLY_RE = /^L([0-9]+)(?:-L?([0-9]+))?$/;

// 按 symbolId 在当前页面会话内缓存聊天记录，切换 Tab 或重开时不丢失上下文。
const chatCache = new Map<string, ChatMessage[]>();

function formatRange(start?: number, end?: number): string {
  if (!start) {
    return "";
  }
  return end && end !== start ? `${start}-${end}` : String(start);
}

function sourceLabel(source: ArchitectureSourceRef): string {
  if (source.symbolName) {
    return source.symbolName;
  }
  if (source.qualifiedName) {
    const parts = source.qualifiedName.split(".").filter(Boolean);
    return parts[parts.length - 1] ?? source.qualifiedName;
  }
  return source.filePath ?? "核心源码";
}

function createWelcomeMessage(label: string): ChatMessage {
  return {
    id: "welcome",
    role: "assistant",
    content: `已载入 \`${label}\`（只读 Agent 模式）。支持流式回答、跨文件追踪调用链，点击回答中的行号或 \`文件:行号\` 可直接高亮跳转源码。`,
    welcome: true,
  };
}

function tryRenderAnchor(
  rawText: string,
  key: number | string,
  callbacks: AnchorCallbacks,
): ReactNode | null {
  const fileMatch = rawText.match(FILE_LINE_RE);
  if (fileMatch && callbacks.onJumpToFileLine) {
    const filePath = fileMatch[1];
    const start = Number.parseInt(fileMatch[2], 10);
    const end = fileMatch[3] ? Number.parseInt(fileMatch[3], 10) : start;
    return (
      <button
        key={key}
        type="button"
        className="chat-anchor chat-anchor--file"
        onClick={() => callbacks.onJumpToFileLine?.(filePath, start, end)}
        title={`跳转到 ${filePath} 第 ${start} 行`}
      >
        {rawText}
      </button>
    );
  }
  const lineMatch = rawText.match(LINE_ONLY_RE);
  if (lineMatch && callbacks.onJumpToLine) {
    const start = Number.parseInt(lineMatch[1], 10);
    const end = lineMatch[2] ? Number.parseInt(lineMatch[2], 10) : start;
    return (
      <button
        key={key}
        type="button"
        className="chat-anchor"
        onClick={() => callbacks.onJumpToLine?.(start, end)}
        title={`滚动并高亮第 ${start}${end > start ? `-${end}` : ""} 行`}
      >
        {rawText}
      </button>
    );
  }
  return null;
}

function renderInlineMarkdown(
  text: string,
  callbacks: AnchorCallbacks,
): ReactNode[] {
  const tokens = text.split(INLINE_TOKEN_RE);
  return tokens.map((token, index) => {
    if (!token) {
      return null;
    }
    if (token.startsWith("`") && token.endsWith("`") && token.length > 2) {
      const inner = token.slice(1, -1);
      const anchor = tryRenderAnchor(inner, index, callbacks);
      if (anchor) {
        return anchor;
      }
      return (
        <code key={index} className="chat-md__inline-code">
          {inner}
        </code>
      );
    }
    if (token.startsWith("**") && token.endsWith("**") && token.length > 4) {
      return <strong key={index}>{token.slice(2, -2)}</strong>;
    }
    const directAnchor = tryRenderAnchor(token, index, callbacks);
    if (directAnchor) {
      return directAnchor;
    }
    return <Fragment key={index}>{token}</Fragment>;
  });
}

function renderMarkdownBlocks(
  content: string,
  callbacks: AnchorCallbacks,
): ReactNode {
  const segments = content.split(/(```[\s\S]*?```)/g);
  return segments.map((segment, segIndex) => {
    if (segment.startsWith("```") && segment.endsWith("```")) {
      const inner = segment.slice(3, -3).replace(/^\n/, "");
      const newlineIndex = inner.indexOf("\n");
      const firstLine =
        newlineIndex >= 0 ? inner.slice(0, newlineIndex).trim() : "";
      const hasLang = /^[a-zA-Z0-9_-]{1,20}$/.test(firstLine);
      const code = hasLang ? inner.slice(newlineIndex + 1) : inner;
      return (
        <pre key={`code-${segIndex}`} className="chat-md__pre">
          <code>{code.replace(/\n$/, "")}</code>
        </pre>
      );
    }

    const lines = segment.split("\n");
    const blocks: ReactNode[] = [];
    let listItems: string[] = [];

    const flushList = (keyPrefix: string) => {
      if (listItems.length === 0) {
        return;
      }
      blocks.push(
        <ul key={`ul-${keyPrefix}`} className="chat-md__list">
          {listItems.map((item, idx) => (
            <li key={idx}>{renderInlineMarkdown(item, callbacks)}</li>
          ))}
        </ul>,
      );
      listItems = [];
    };

    lines.forEach((rawLine, lineIndex) => {
      const line = rawLine.trimEnd();
      const trimmed = line.trim();
      if (!trimmed) {
        flushList(`${segIndex}-${lineIndex}`);
        return;
      }
      const listMatch = trimmed.match(/^(?:[-*+]|\d+\.)\s+(.*)$/);
      if (listMatch) {
        listItems.push(listMatch[1]);
        return;
      }
      flushList(`${segIndex}-${lineIndex}`);
      const headingMatch = trimmed.match(/^(#{1,4})\s+(.*)$/);
      if (headingMatch) {
        blocks.push(
          <div key={`h-${segIndex}-${lineIndex}`} className="chat-md__heading">
            {renderInlineMarkdown(headingMatch[2], callbacks)}
          </div>,
        );
        return;
      }
      blocks.push(
        <p key={`p-${segIndex}-${lineIndex}`} className="chat-md__p">
          {renderInlineMarkdown(line, callbacks)}
        </p>,
      );
    });

    flushList(`${segIndex}-end`);
    return <Fragment key={`seg-${segIndex}`}>{blocks}</Fragment>;
  });
}

export function SourceChatPanel({
  source,
  nodeLabel,
  configured,
  onJumpToLine,
  onJumpToFileLine,
}: SourceChatPanelProps) {
  const label = sourceLabel(source);
  const range = formatRange(source.startLine, source.endLine);
  const cacheKey = source.symbolId ?? source.filePath ?? label;

  const [messages, setMessages] = useState<ChatMessage[]>(() => {
    const cached = chatCache.get(cacheKey);
    return cached && cached.length > 0 ? cached : [createWelcomeMessage(label)];
  });
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const scrollAnchorRef = useRef<HTMLDivElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const cached = chatCache.get(cacheKey);
    setMessages(
      cached && cached.length > 0 ? cached : [createWelcomeMessage(label)],
    );
    setDraft("");
    setSending(false);
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, [cacheKey, label]);

  useEffect(() => {
    const stable = messages.filter((item) => !item.pending);
    if (stable.length > 0) {
      chatCache.set(cacheKey, stable);
    }
  }, [cacheKey, messages]);

  useEffect(() => {
    scrollAnchorRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  const hasConversation = messages.some((item) => !item.welcome);
  const anchorCallbacks: AnchorCallbacks = { onJumpToLine, onJumpToFileLine };

  const handleClear = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setSending(false);
    const initial = [createWelcomeMessage(label)];
    chatCache.set(cacheKey, initial);
    setMessages(initial);
  };

  const handleStop = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setSending(false);
    setMessages((current) =>
      current.map((item) =>
        item.pending
          ? {
              ...item,
              content: item.content ? `${item.content}\n\n（已停止生成）` : "已停止生成。",
              pending: false,
            }
          : item,
      ),
    );
  };

  const submit = async (overrideMessage?: string) => {
    const message = (overrideMessage ?? draft).trim();
    if (!message || sending || !configured || !source.symbolId) {
      return;
    }

    const history: ChatHistoryItem[] = messages
      .filter((item) => !item.welcome && !item.pending && !item.error)
      .slice(-12)
      .map((item) => ({
        role: item.role,
        content: item.content,
      }));

    const userId = `user-${Date.now()}`;
    const pendingId = `pending-${Date.now()}`;
    if (!overrideMessage) {
      setDraft("");
    }
    setSending(true);
    setMessages((current) => [
      ...current,
      { id: userId, role: "user", content: message },
      {
        id: pendingId,
        role: "assistant",
        content: "",
        pending: true,
        toolsUsed: [],
      },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamChatReply(
        {
          message,
          symbolId: source.symbolId,
          nodeLabel,
          history,
        },
        {
          onTool: (toolName) => {
            setMessages((current) =>
              current.map((item) =>
                item.id === pendingId
                  ? {
                      ...item,
                      toolsUsed: [...(item.toolsUsed ?? []), toolName],
                    }
                  : item,
              ),
            );
          },
          onDelta: (chunk) => {
            setMessages((current) =>
              current.map((item) =>
                item.id === pendingId
                  ? { ...item, content: item.content + chunk }
                  : item,
              ),
            );
          },
          onDone: (payload) => {
            setMessages((current) =>
              current.map((item) =>
                item.id === pendingId
                  ? {
                      ...item,
                      content: payload.message || item.content || "模型没有返回文本回答。",
                      pending: false,
                      toolsUsed:
                        payload.toolsUsed.length > 0
                          ? payload.toolsUsed
                          : item.toolsUsed,
                      ttftMs: payload.ttftMs,
                      durationMs: payload.durationMs,
                    }
                  : item,
              ),
            );
          },
        },
        controller.signal,
      );
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        return;
      }
      setMessages((current) =>
        current.map((item) =>
          item.id === pendingId
            ? {
                ...item,
                content: architectureErrorMessage(error),
                pending: false,
                error: true,
              }
            : item,
        ),
      );
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
      setSending(false);
    }
  };

  return (
    <aside className="source-chat" aria-label="AI 源码对话">
      <header className="source-chat__header">
        <div>
          <div className="eyebrow">AI AGENT CHAT · SSE</div>
          <h2 className="source-chat__title">和这段源码对话</h2>
        </div>
        <div className="source-chat__header-actions">
          {hasConversation ? (
            <button
              type="button"
              className="ghost-button"
              onClick={handleClear}
              title="清空当前符号的对话历史"
            >
              清空
            </button>
          ) : null}
          <span
            className={`source-chat__status${configured ? " is-ready" : ""}`}
          >
            <i aria-hidden="true" />
            {configured ? "AGENT READY" : "AI 未配置"}
          </span>
        </div>
      </header>

      <div className="source-chat__context">
        <span className="source-chat__context-mark" aria-hidden="true">
          ◆
        </span>
        <div className="source-chat__context-copy">
          <strong>{label}</strong>
          <span>
            {nodeLabel && nodeLabel !== label ? `${nodeLabel} · ` : ""}
            {source.filePath ?? "未知源码路径"}
            {range ? `:${range}` : ""}
          </span>
        </div>
      </div>

      {configured && !hasConversation ? (
        <div className="source-chat__quick-prompts" aria-label="快捷提问">
          {QUICK_PROMPTS.map((prompt) => (
            <button
              key={prompt}
              type="button"
              className="source-chat__quick-chip"
              disabled={sending}
              onClick={() => void submit(prompt)}
            >
              {prompt}
            </button>
          ))}
        </div>
      ) : null}

      <div className="source-chat__messages" aria-live="polite">
        {messages.map((message) => (
          <div
            key={message.id}
            className={`source-chat__message source-chat__message--${message.role}${
              message.error ? " is-error" : ""
            }`}
          >
            {message.role === "assistant" ? (
              <span className="source-chat__avatar" aria-hidden="true">
                ✦
              </span>
            ) : null}
            <div className="source-chat__bubble">
              {message.toolsUsed && message.toolsUsed.length > 0 ? (
                <div className="source-chat__tools" aria-label="Agent 调用的只读工具">
                  {message.toolsUsed.map((tool, idx) => (
                    <span
                      key={`${tool}-${idx}`}
                      className="source-chat__tool-pill"
                    >
                      ⚙ {tool}
                    </span>
                  ))}
                </div>
              ) : null}

              {message.content ? (
                message.role === "assistant" && !message.error ? (
                  <div className="chat-md">
                    {renderMarkdownBlocks(message.content, anchorCallbacks)}
                  </div>
                ) : (
                  message.content
                )
              ) : null}

              {message.pending ? (
                <span className="source-chat__typing" aria-label="AI 正在思考">
                  <i />
                  <i />
                  <i />
                </span>
              ) : null}

              {!message.pending &&
              !message.welcome &&
              !message.error &&
              message.role === "assistant" &&
              (message.ttftMs || message.durationMs) ? (
                <div className="source-chat__meta">
                  {message.ttftMs ? `首字 ${message.ttftMs}ms` : ""}
                  {message.ttftMs && message.durationMs ? " · " : ""}
                  {message.durationMs ? `总计 ${(message.durationMs / 1000).toFixed(1)}s` : ""}
                </div>
              ) : null}
            </div>
          </div>
        ))}
        <div ref={scrollAnchorRef} />
      </div>

      <form
        className="source-chat__composer"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <textarea
          className="source-chat__input"
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              void submit();
            }
          }}
          placeholder={
            configured
              ? "可追问当前源码或跨文件调用链…"
              : "当前没有可用的 AI Provider"
          }
          aria-label="输入源码问题"
          rows={3}
          disabled={!configured || sending}
        />
        <div className="source-chat__composer-footer">
          <span className="source-chat__shortcut">
            Enter 发送 · Shift + Enter 换行
          </span>
          {sending ? (
            <button
              type="button"
              className="source-chat__stop"
              onClick={handleStop}
            >
              停止
            </button>
          ) : (
            <button
              type="submit"
              className="source-chat__send"
              disabled={!configured || !draft.trim()}
            >
              发送
            </button>
          )}
        </div>
      </form>
      <p className="source-chat__hint">
        只读 Agent（可调用 CodeGraph/FTS5 跨文件检索）· 严禁修改项目文件
      </p>
    </aside>
  );
}
