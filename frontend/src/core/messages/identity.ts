import type { ChatMessage, MessageType } from "../types";

/**
 * M6 的动态上下文中间件把提交的用户消息从 `X` 重命名为 `X__user`。
 *
 * UI 的身份匹配**只对人类消息**归一化这个保留后缀，这样"提交帧"和
 * "checkpoint 替换"才会被认成同一个可见轮次（M5 §4.4）。
 *
 * 对 AI/tool 消息做同样的归一化是错的：tool_call_id 里出现 `__user` 只是巧合，
 * 而归一化会把两条不同的工具结果合并成一条。
 */
export const DYNAMIC_CONTEXT_USER_SUFFIX = "__user";

export function normalizeIdentity(id: string, type: MessageType): string {
  if (type !== "human") return id;
  return id.endsWith(DYNAMIC_CONTEXT_USER_SUFFIX)
    ? id.slice(0, -DYNAMIC_CONTEXT_USER_SUFFIX.length)
    : id;
}

/** 合并/去重用的身份键。同一个 key 的多个副本是"同一条消息的不同副本" */
export function messageKey(message: Pick<ChatMessage, "id" | "type">): string {
  return normalizeIdentity(message.id, message.type);
}

/** 工具结果按 tool_call_id 回填时用（不参与列表身份） */
export function toolResultKey(message: Pick<ChatMessage, "tool_call_id">): string | null {
  return message.tool_call_id ?? null;
}
