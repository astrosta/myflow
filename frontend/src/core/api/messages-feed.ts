import type { ChatMessage, MessageType } from "../types";
import type { MessagePage } from "../messages/history";
import { apiUrl } from "./base";

/**
 * 分页拉取的消息 feed（M5 §4.1 的"持久化历史"那一列）。
 *
 * 后端契约（router-layer 文档 + 实测 openapi）：
 *
 *   GET /api/threads/{tid}/messages?limit=&before_seq=&after_seq=
 *   -> Page[MessageResponse] = { data: [...], next_cursor: string|null, has_more: bool }
 *
 * MessageResponse = { thread_id, run_id, seq, event_type, content, metadata, created_at }
 *
 * **它不是一个 LangChain 消息**：没有 message id、没有 tool_calls。
 * 这是 run_events 账本里 category="message" 的那几行（M4 §4.3）。
 * 所以身份只能从 seq 推，event_type 决定它是什么角色。
 */

export const DEFAULT_PAGE_SIZE = 50;

/** 账本里 category="message" 的三个 event_type（agentkit/runtime/events/catalog.py） */
export const MESSAGE_EVENT_TYPES: Record<string, MessageType> = {
  "llm.human.input": "human",
  "llm.ai.response": "ai",
  "llm.tool.result": "tool",
};

export interface FeedMessage {
  thread_id: string;
  run_id: string;
  seq: number;
  event_type: string;
  content: unknown;
  metadata: Record<string, unknown>;
  created_at: string | null;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** content 是 str | dict（journal 的 _message_content 保证），但再兜一层 */
function normalizeContent(value: unknown): ChatMessage["content"] {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value as ChatMessage["content"];
  if (isRecord(value)) {
    const inner = value.content;
    if (typeof inner === "string") return inner;
    if (Array.isArray(inner)) return inner as ChatMessage["content"];
    if (typeof value.text === "string") return value.text;
    return JSON.stringify(value);
  }
  return value === null || value === undefined ? "" : String(value);
}

/**
 * 账本行 -> UI 的消息。
 *
 * **id 由 seq 合成**：账本行没有 message id，而 seq 在一个 thread 内严格递增、
 * 永不复用，正是我们要的稳定合并身份（M5 §4.2 的位置权威也挂在它上面）。
 * 用 seq 当身份还顺带保证了：同一条消息无论从哪一页回来，key 都一样。
 */
export function feedMessageToChatMessage(row: FeedMessage): ChatMessage | null {
  const type = MESSAGE_EVENT_TYPES[row.event_type];
  if (!type) return null; // 非消息事件（trace / outputs）不该出现在这个端点

  /**
   * **身份优先用 content 里那个 LangChain 消息 id。**
   *
   * 账本行的 content 是完整序列化后的消息对象，里面带 `id`——和实时流里
   * 那条消息是同一个 id。用它当身份，历史副本和流副本才能在 §4 的合并里
   * 被认成同一条；只按 seq 合成的话两边永远对不上，屏幕上就是同一份内容
   * 出现两遍（流一遍、账本一遍）。
   *
   * seq 只是兜底；它照旧写进 `seq` 字段，位置权威仍按 §4.2 走。
   */
  const inner = isRecord(row.content) ? row.content : null;
  const innerId = inner && typeof inner.id === "string" && inner.id ? inner.id : null;

  const message: ChatMessage = {
    id: innerId ?? "evt-" + String(row.seq),
    type,
    content: normalizeContent(row.content),
    seq: typeof row.seq === "number" ? row.seq : null,
    run_id: row.run_id ?? null,
    created_at: row.created_at ?? null,
  };

  // 工具结果要挂回 tool_calls 上。账本行本身不带 tool_call_id，
  // 但 metadata 里可能有（M4 §4.3 的信封允许扩展）。
  const callId = row.metadata?.tool_call_id;
  if (typeof callId === "string") message.tool_call_id = callId;
  const toolName = row.metadata?.name;
  if (typeof toolName === "string") message.name = toolName;

  // AI 消息的工具调用：content 是 dict 且带 tool_calls 时取出来
  if (isRecord(row.content) && Array.isArray(row.content.tool_calls)) {
    message.tool_calls = row.content.tool_calls as ChatMessage["tool_calls"];
  }
  return message;
}

export interface FetchMessagePageOptions {
  limit?: number;
  /** 取更旧的一页（M4 §5.1 的双向游标） */
  beforeSeq?: number | null;
  /** 取更新的一页。gap 恢复后补洞用 */
  afterSeq?: number | null;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

/** 形状不对就抛——静默返回空数组会把"历史没了"伪装成"这个会话是空的" */
export function parseMessagePage(payload: unknown): MessagePage {
  if (!isRecord(payload) || !Array.isArray(payload.data)) {
    throw new Error("Unexpected /messages payload: expected { data: [...] }");
  }
  const rows = payload.data.filter(isRecord) as unknown as FeedMessage[];
  const messages = rows
    .map(feedMessageToChatMessage)
    .filter((m): m is ChatMessage => m !== null);

  const hasMore = payload.has_more === true;
  const cursor = typeof payload.next_cursor === "string" ? Number(payload.next_cursor) : null;
  const seqs = messages.map((m) => m.seq).filter((s): s is number => typeof s === "number");

  return {
    messages,
    hasMoreBefore: hasMore,
    // next_cursor 就是本页最后一条的 seq（后端 message_page 里 str(wired[-1].seq)）
    nextBeforeSeq: Number.isFinite(cursor) ? cursor : (seqs.length ? Math.max(...seqs) : null),
  };
}

export async function fetchMessagePage(
  threadId: string,
  options: FetchMessagePageOptions = {},
): Promise<MessagePage> {
  const { limit = DEFAULT_PAGE_SIZE, beforeSeq = null, afterSeq = null, signal, fetchImpl = fetch } = options;
  const params = new URLSearchParams({ limit: String(limit) });
  if (beforeSeq !== null) params.set("before_seq", String(beforeSeq));
  if (afterSeq !== null) params.set("after_seq", String(afterSeq));

  const response = await fetchImpl(
    apiUrl("/threads/" + encodeURIComponent(threadId) + "/messages?" + params.toString()),
    { signal },
  );
  if (!response.ok) {
    throw new Error("Failed to load messages for thread " + threadId + ": HTTP " + response.status);
  }
  return parseMessagePage(await response.json());
}
