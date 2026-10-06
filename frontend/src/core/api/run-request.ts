import { rewriteStreamModes } from "./stream-mode";

/**
 * 网关的 RunCreateRequest 是 extra="forbid"（router-layer 文档明确写着
 * "unknown SDK fields are 422"）。SDK 会往里塞自己的开关，实测被拒的是：
 *
 *   {"loc":["body","stream_resumable"],"msg":"Run option stream_resumable is not supported"}
 *
 * 而且 stream_resumable 在 schema 里是 const false —— SDK 传 true 必炸。
 *
 * 所以这一层做**白名单裁剪**：只放行网关认得、且我们真的要用的字段。
 * 这比逐个拉黑稳：SDK 升级时新加的开关不会再把发送搞挂。
 * 被丢掉的都是我们没在用的特性（webhook / on_completion / stream_resumable…）。
 */
const RUN_CREATE_ALLOWED = new Set([
  "assistant_id",
  "input",
  "command",
  "metadata",
  "config",
  "context",
  "checkpoint_id",
  "checkpoint",
  "stream_mode",
  "stream_subgraphs",
  "on_disconnect",
  "multitask_strategy",
  "if_not_exists",
]);

export const RUN_REQUEST_FIELDS = RUN_CREATE_ALLOWED;

/** 只有这三条创建路由用 RunCreateRequest */
export function isRunCreateRequest(path: string): boolean {
  return /\/runs(?:\/stream|\/wait)?\/?$/.test(path);
}

/**
 * 返回处理后的 body；没改动就返回原字符串（调用方据此避免重建对象）。
 *
 * assistant_id 是必填，缺了直接抛——让"没配 assistant"在客户端就暴露，
 * 而不是变成一个看不懂的 422。
 */
export function sanitizeRunCreateBody(jsonBody: string): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(jsonBody);
  } catch {
    return jsonBody;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return jsonBody;

  const body = parsed as Record<string, unknown>;
  if (typeof body.assistant_id !== "string" || !body.assistant_id) {
    throw new Error("run 请求缺少 assistant_id");
  }

  const kept: Record<string, unknown> = {};
  for (const key of RUN_CREATE_ALLOWED) {
    if (body[key] !== undefined) kept[key] = body[key];
  }

  // stream_mode 还要再过一遍增量模式收窄（§3.1）
  const rewritten = rewriteStreamModes(JSON.stringify(kept));
  return rewritten;
}
