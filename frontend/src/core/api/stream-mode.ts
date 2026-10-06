/**
 * 流模式的显式收窄（M5 §3.1）。
 *
 * SDK 支持多种 `stream_mode`。**主聊天流要强制用增量模式**：
 * `messages-tuple + updates + custom`，**不要带 `values`**。
 * `values` 是全量状态快照——每一帧都把整个 `ThreadState` 重发一遍。
 * 在一次几十轮的 run 里，这是几十次重复传输整个对话历史。
 */

/**
 * 主聊天流的模式集合。
 *
 * **`messages-tuple` 不能省。** 后端的文件工具分块合批（M3 里那个把
 * `write_file` 的逐 token 输出攒成大块的优化）**是按 `messages-tuple` 触发的**，
 * 不是 `values`。去掉它，写一个大文件会退化成每个 token 一帧 SSE。
 */
export const INCREMENTAL_STREAM_MODES = ["messages-tuple", "updates", "custom"] as const;

/** 网关认得的全部模式。不在这里的一律抛错，不做"部分转发" */
export const SUPPORTED_STREAM_MODES: ReadonlySet<string> = new Set([
  ...INCREMENTAL_STREAM_MODES,
  "values",
  "debug",
  "tasks",
  "checkpoints",
]);

/** 显式的状态检查（gap 恢复后重载）仍然用 `values` */
export const STATE_INSPECTION_STREAM_MODES = ["values"] as const;

export class UnsupportedStreamModeError extends Error {
  readonly unsupported: readonly string[];

  constructor(unsupported: readonly string[]) {
    super(
      `Unsupported stream_mode: ${unsupported.join(", ")}. ` +
        `Supported: ${[...SUPPORTED_STREAM_MODES].sort().join(", ")}`,
    );
    this.name = "UnsupportedStreamModeError";
    this.unsupported = unsupported;
  }
}

/**
 * 请求前净化：遇到 Gateway 不支持的模式**直接抛错**。
 *
 * 不要"部分转发"，也不要"静默退回 `values`"——后者会让一个拼错的模式名
 * 表现为"莫名其妙变慢了"。
 */
export function assertSupportedStreamModes(modes: readonly string[]): void {
  const unsupported = modes.filter((mode) => !SUPPORTED_STREAM_MODES.has(mode));
  if (unsupported.length) throw new UnsupportedStreamModeError(unsupported);
}

export interface ChatStreamModeOptions {
  /** 用户显式请求的非快照模式（调试、任务、检查点）保留 */
  extra?: readonly string[];
  /**
   * 显式的状态检查（§5.2 gap 恢复后的重载）。
   * 这是 §3.1 那个"仍然用 `values`"的例外，**必须显式传 true**。
   */
  includeValues?: boolean;
}

/**
 * 把 SDK 实际会发出去的模式列表覆盖成增量模式。
 *
 * 注意是**强制覆盖**，不是"不请求它"：SDK 可能会因为惰性追踪自己加上 `values`。
 * 所以这个函数接收的是"将要发出去的那份"，返回"真正发出去的那份"。
 */
export function sanitizeChatStreamModes(
  requested: readonly string[] | null | undefined,
  options: ChatStreamModeOptions = {},
): string[] {
  const incoming = requested ?? [];
  assertSupportedStreamModes(incoming);
  assertSupportedStreamModes(options.extra ?? []);

  const modes = new Set<string>(INCREMENTAL_STREAM_MODES);
  for (const mode of options.extra ?? []) modes.add(mode);
  if (options.includeValues) modes.add("values");
  else modes.delete("values");

  // 增量模式永远在，无论调用方想不想要——它们是这份协议的最小可用集
  return [...modes];
}

/** 判断一份模式列表是否已经被收窄成增量模式（给断言和调试用） */
export function isIncrementalOnly(modes: readonly string[]): boolean {
  return !modes.includes("values");
}

/**
 * 只对流式 run 的创建请求做覆盖。
 *
 * 显式的状态检查（`GET /state`、gap 恢复）不在这个范围里——那是 §3.1 的例外，
 * 它们的 `values` 必须留着。
 */
export function isStreamingRunRequest(path: string): boolean {
  return /\/runs\/stream\/?$/.test(path) || /\/runs\/[^/]+\/stream\/?$/.test(path);
}

/**
 * 在请求真正发出去之前，把 body 里的 `stream_mode` **覆盖**成增量模式（§3.1）。
 *
 * "强制覆盖"和"不请求它"是两件事：SDK 可能会因为惰性追踪自己加上 `values`，
 * 所以必须在最后一刻改掉即将上线的那份 body，而不是指望调用方不要传。
 *
 * 返回原字符串表示没有改动（调用方据此避免无谓的对象重建）。
 */
export function rewriteStreamModes(jsonBody: string): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(jsonBody);
  } catch {
    return jsonBody; // 不是 JSON 就不是我们要管的请求
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return jsonBody;

  const body = parsed as Record<string, unknown>;
  const declared = body.stream_mode;
  const requested = Array.isArray(declared)
    ? declared.filter((mode): mode is string => typeof mode === "string")
    : typeof declared === "string"
      ? [declared]
      : null;

  // 调用方没声明模式时也要补上：默认值在服务端是 `values`（那个全量快照）
  const sanitized = sanitizeChatStreamModes(requested);
  if (Array.isArray(declared) && declared.length === sanitized.length) {
    const same = sanitized.every((mode) => declared.includes(mode));
    if (same) return jsonBody;
  }
  return JSON.stringify({ ...body, stream_mode: sanitized });
}
