import type { ChatMessage, ToolCall } from "../types";
import type { SubagentView } from "./subagent";

/** 工具结果按 `tool_call_id` 回填（M5 §2 的 core/messages 职责之一） */
export function indexToolResults(
  messages: readonly ChatMessage[],
): Map<string, ChatMessage> {
  const index = new Map<string, ChatMessage>();
  for (const message of messages) {
    if (message.type !== "tool") continue;
    const id = message.tool_call_id;
    if (!id) continue;
    // 同一个 call 收到多条结果时，保留最新的一条（重试会再发一次）
    index.set(id, message);
  }
  return index;
}

export type ToolCallStatus = "pending" | "success" | "error";

export interface ToolCallView {
  id: string;
  name: string;
  args: Record<string, unknown>;
  /** 流式期间的半截 JSON。解析失败时保住原文，别丢给 `JSON.parse` 炸掉 */
  argsText?: string;
  status: ToolCallStatus;
  result?: ChatMessage;
  resultText: string;
  /** task 工具专用：由台账/实时事件/工具结果合成，普通工具没有这个字段 */
  subagent?: SubagentView;
}

export function resolveToolCalls(
  message: ChatMessage,
  results: ReadonlyMap<string, ChatMessage>,
  fallbackKeyPrefix = "call",
): ToolCallView[] {
  return (message.tool_calls ?? []).map((call: ToolCall, index: number): ToolCallView => {
    const id = call.id ?? `${fallbackKeyPrefix}-${message.id}-${index}`;
    const result = results.get(id);
    const status: ToolCallStatus = result
      ? result.status === "error" || result.additional_kwargs?.is_error === true
        ? "error"
        : "success"
      : "pending";
    return {
      id,
      name: call.name ?? "unknown_tool",
      args: call.args ?? {},
      ...(call.argsText !== undefined ? { argsText: call.argsText } : {}),
      status,
      ...(result ? { result } : {}),
      resultText: result ? textOf(result) : "",
    };
  });
}

export function textOf(message: ChatMessage): string {
  const { content } = message;
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((block) =>
      typeof block?.text === "string" ? block.text : typeof block?.content === "string" ? block.content : "",
    )
    .filter(Boolean)
    .join("\n");
}

/** 工具参数的可读渲染。流式期间 args 还是半截 JSON，退回原文 */
export function formatToolArgs(call: ToolCallView): string {
  if (call.args && Object.keys(call.args).length) {
    try {
      return JSON.stringify(call.args, null, 2);
    } catch {
      /* 循环引用等，落到下面 */
    }
  }
  return call.argsText ?? "";
}
