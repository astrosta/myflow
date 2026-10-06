import type { ChatMessage, ContentBlock, MessageType, ToolCall } from "../types";

/**
 * SDK 的 `Message` → UI 的 `ChatMessage`。
 *
 * 转换只在这一个边界上发生，往里的都是纯函数（M5 §2）。
 */

const MESSAGE_TYPES: ReadonlySet<string> = new Set(["human", "ai", "tool", "system"]);

function asType(value: unknown): MessageType {
  return typeof value === "string" && MESSAGE_TYPES.has(value) ? (value as MessageType) : "ai";
}

/**
 * `seq` 是**服务端拥有的展示元数据**（§4.2）。
 *
 * M3/M4 还没落地，它具体落在哪个字段上还没定死，所以这里把几个合理落点都认一遍。
 * 认不出来的后果是排序退回"身份锚点编织"——能用，但没有 seq 优先那么确定。
 */
export function readSeq(message: unknown, tupleMetadata?: unknown): number | null {
  const candidates: unknown[] = [];
  if (typeof message === "object" && message !== null) {
    const bag = message as Record<string, unknown>;
    candidates.push(bag.seq);
    if (typeof bag.additional_kwargs === "object" && bag.additional_kwargs !== null) {
      candidates.push((bag.additional_kwargs as Record<string, unknown>).seq);
    }
    if (typeof bag.response_metadata === "object" && bag.response_metadata !== null) {
      candidates.push((bag.response_metadata as Record<string, unknown>).seq);
    }
  }
  if (typeof tupleMetadata === "object" && tupleMetadata !== null) {
    candidates.push((tupleMetadata as Record<string, unknown>).seq);
  }
  for (const candidate of candidates) {
    if (typeof candidate === "number" && Number.isFinite(candidate)) return candidate;
  }
  return null;
}

function readRunId(message: unknown, tupleMetadata?: unknown): string | null {
  const sources = [message, tupleMetadata];
  for (const source of sources) {
    if (typeof source !== "object" || source === null) continue;
    const bag = source as Record<string, unknown>;
    for (const key of ["run_id", "runId"]) {
      if (typeof bag[key] === "string") return bag[key];
    }
    const metadata = bag.metadata;
    if (typeof metadata === "object" && metadata !== null) {
      const runId = (metadata as Record<string, unknown>).run_id;
      if (typeof runId === "string") return runId;
    }
  }
  return null;
}

function normalizeContent(value: unknown): string | ContentBlock[] {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value as ContentBlock[];
  return "";
}

function normalizeToolCalls(value: unknown): ToolCall[] | undefined {
  if (!Array.isArray(value) || !value.length) return undefined;
  return value.map((raw): ToolCall => {
    const bag = (typeof raw === "object" && raw !== null ? raw : {}) as Record<string, unknown>;
    const call: ToolCall = {};
    if (typeof bag.id === "string") call.id = bag.id;
    if (typeof bag.name === "string") call.name = bag.name;
    if (typeof bag.args === "object" && bag.args !== null) {
      call.args = bag.args as Record<string, unknown>;
    } else if (typeof bag.args === "string") {
      // 流式期间 args 是半截 JSON，原样保住
      call.argsText = bag.args;
    }
    return call;
  });
}

const FALLBACK_PREFIX = "msg";

/**
 * `id` 是合并与去重的身份。SDK 的 `Message.id` 是可选的，
 * 缺了就用"在列表里的位置 + 类型"合成一个稳定键——
 * 不稳定会让同一条消息在每次渲染时被当成新消息，列表会抖。
 */
export function toChatMessage(
  message: unknown,
  index: number,
  tupleMetadata?: unknown,
): ChatMessage {
  const bag = (typeof message === "object" && message !== null ? message : {}) as Record<string, unknown>;
  const type = asType(bag.type);
  const id = typeof bag.id === "string" && bag.id ? bag.id : `${FALLBACK_PREFIX}-${type}-${index}`;

  const out: ChatMessage = {
    id,
    type,
    content: normalizeContent(bag.content),
    seq: readSeq(message, tupleMetadata),
    run_id: readRunId(message, tupleMetadata),
  };
  if (typeof bag.name === "string") out.name = bag.name;
  if (typeof bag.tool_call_id === "string") out.tool_call_id = bag.tool_call_id;
  if (bag.status === "success" || bag.status === "error") out.status = bag.status;
  const toolCalls = normalizeToolCalls(bag.tool_calls);
  if (toolCalls) out.tool_calls = toolCalls;
  if (typeof bag.additional_kwargs === "object" && bag.additional_kwargs !== null) {
    out.additional_kwargs = bag.additional_kwargs as Record<string, unknown>;
  }
  if (typeof bag.response_metadata === "object" && bag.response_metadata !== null) {
    out.response_metadata = bag.response_metadata as Record<string, unknown>;
  }
  return out;
}

export function toChatMessages(messages: readonly unknown[] | undefined): ChatMessage[] {
  if (!messages?.length) return [];
  return messages.map((message, index) => toChatMessage(message, index));
}
