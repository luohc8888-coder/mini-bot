import { useEffect, useRef, useState } from "react";
import {
  architectureErrorMessage,
  fetchChatReply,
} from "../architecture/api";
import type { ArchitectureSourceRef } from "../types/architecture";

interface SourceChatPanelProps {
  source: ArchitectureSourceRef;
  nodeLabel?: string;
  configured: boolean;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  pending?: boolean;
  error?: boolean;
}

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

export function SourceChatPanel({
  source,
  nodeLabel,
  configured,
}: SourceChatPanelProps) {
  const label = sourceLabel(source);
  const range = formatRange(source.startLine, source.endLine);
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: "welcome",
      role: "assistant",
      content: `已载入 ${label}。可以问我这段源码的职责、关键流程或潜在风险。`,
    },
  ]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const scrollAnchorRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    scrollAnchorRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  const submit = async () => {
    const message = draft.trim();
    if (!message || sending || !configured || !source.symbolId) {
      return;
    }
    const userId = `user-${Date.now()}`;
    const pendingId = `pending-${Date.now()}`;
    setDraft("");
    setSending(true);
    setMessages((current) => [
      ...current,
      { id: userId, role: "user", content: message },
      { id: pendingId, role: "assistant", content: "", pending: true },
    ]);

    try {
      const reply = await fetchChatReply({
        message,
        symbolId: source.symbolId,
        nodeLabel,
      });
      setMessages((current) =>
        current.map((item) =>
          item.id === pendingId
            ? { ...item, content: reply.message, pending: false }
            : item,
        ),
      );
    } catch (error) {
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
      setSending(false);
    }
  };

  return (
    <aside className="source-chat" aria-label="AI 源码对话">
      <header className="source-chat__header">
        <div>
          <div className="eyebrow">AI CODE CHAT</div>
          <h2 className="source-chat__title">和这段源码对话</h2>
        </div>
        <span
          className={`source-chat__status${configured ? " is-ready" : ""}`}
        >
          <i aria-hidden="true" />
          {configured ? "AI READY" : "AI 未配置"}
        </span>
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
              {message.pending ? (
                <span className="source-chat__typing" aria-label="AI 正在思考">
                  <i />
                  <i />
                  <i />
                </span>
              ) : (
                message.content
              )}
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
              ? "询问这段源码的职责、流程或风险…"
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
          <button
            type="submit"
            className="source-chat__send"
            disabled={!configured || sending || !draft.trim()}
          >
            {sending ? "思考中…" : "发送"}
          </button>
        </div>
      </form>
      <p className="source-chat__hint">仅发送当前符号的源码片段 · 不会执行源码</p>
    </aside>
  );
}
