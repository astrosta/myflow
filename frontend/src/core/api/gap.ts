import type { StreamGapFrame } from "../types";

/**
 * SSE 的 gap 帧要自己包一层（M5 §5.2）。
 *
 * **上游 SDK 会忽略它不认识的事件名。** 所以包装层必须在 SDK **下面**——
 * 包住原始 SSE 帧，而不是包住 SDK 解析后的输出。包错层的表现是：
 * gap 被 SDK 静默丢掉，界面显示"已完成"，而后端的 run 其实还在跑。
 *
 * 这一层同时包住初始流和 join 的流。
 */

export interface SseFrame {
  event: string;
  data: string;
  id: string | null;
}

/** 解析一段 SSE 字节流。处理注释行（`: heartbeat`）和多行 `data:` */
export async function* parseSseStream(
  source: AsyncIterable<Uint8Array | string>,
): AsyncIterable<SseFrame> {
  const decoder = new TextDecoder();
  let buffer = "";

  for await (const chunk of source) {
    buffer += typeof chunk === "string" ? chunk : decoder.decode(chunk, { stream: true });
    // 帧分隔符是空行。`\r\n\r\n` 也认
    let boundary = findFrameBoundary(buffer);
    while (boundary >= 0) {
      const raw = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary).replace(/^(\r?\n){2}/, "");
      const frame = parseFrame(raw);
      if (frame) yield frame;
      boundary = findFrameBoundary(buffer);
    }
  }

  buffer += decoder.decode();
  const tail = parseFrame(buffer);
  if (tail) yield tail;
}

function findFrameBoundary(buffer: string): number {
  const lf = buffer.indexOf("\n\n");
  const crlf = buffer.indexOf("\r\n\r\n");
  if (lf < 0) return crlf;
  if (crlf < 0) return lf;
  return Math.min(lf, crlf);
}

function parseFrame(raw: string): SseFrame | null {
  let event = "message";
  let id: string | null = null;
  const dataLines: string[] = [];

  for (const line of raw.split(/\r?\n/)) {
    if (!line || line.startsWith(":")) continue; // 注释行：心跳
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    switch (field) {
      case "event":
        event = value;
        break;
      case "data":
        dataLines.push(value);
        break;
      case "id":
        id = value;
        break;
      default:
        break;
    }
  }

  if (!dataLines.length && event === "message") return null;
  return { event, data: dataLines.join("\n"), id };
}

/** 字段顺序 `event` → `data` → `id` → 空行，是 SDK 解码器的约定（M3 §8.1） */
export function formatSseFrame(frame: SseFrame): string {
  let out = `event: ${frame.event}\ndata: ${frame.data}\n`;
  if (frame.id) out += `id: ${frame.id}\n`;
  return out + "\n";
}

const GAP_CODE = "stream_replay_gap";

export function asGapFrame(frame: SseFrame): StreamGapFrame | null {
  if (frame.event !== "gap") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(frame.data);
  } catch {
    parsed = null;
  }
  const bag = (parsed ?? {}) as Record<string, unknown>;
  const asId = (value: unknown): string | null =>
    typeof value === "string" && value.length ? value : null;
  return {
    code: GAP_CODE,
    ...(typeof bag.run_id === "string" ? { run_id: bag.run_id } : {}),
    requested_event_id: asId(bag.requested_event_id),
    earliest_available_event_id: asId(bag.earliest_available_event_id),
    latest_available_event_id: asId(bag.latest_available_event_id),
    recovery: "reload_durable_state",
  };
}

/**
 * §5.2 第 4 条：从服务端给的保留尾部之后续；**缓冲区空了就不带游标重新 join**。
 *
 * `/wait` 用 `latest_available_event_id` 做安全的续订点；浏览器这边同理。
 */
export function nextSubscriptionCursor(gap: StreamGapFrame): string | null {
  return gap.latest_available_event_id ?? null;
}

export interface StreamRecoveryEvent {
  /** 内部事件：通知上层清乐观状态、失效历史缓存、给用户看一个恢复提示 */
  frame: StreamGapFrame;
  attempt: number;
  maxAttempts: number;
  exhausted: boolean;
}

export class StreamRecoveryExhaustedError extends Error {
  readonly frame: StreamGapFrame;

  constructor(frame: StreamGapFrame, attempts: number) {
    super(`SSE replay gap could not be recovered after ${attempts} attempts`);
    this.name = "StreamRecoveryExhaustedError";
    this.frame = frame;
  }
}

export const DEFAULT_MAX_RECOVERIES = 5;

export interface GapRecoveryOptions<TFrame> {
  /** 从一帧里认出 gap。返回 null 表示这不是 gap 帧 */
  classify: (frame: TFrame) => StreamGapFrame | null;
  /** 重新订阅。cursor 为 null 时等价于"从头订" */
  resubscribe: (cursor: string | null) => AsyncIterable<TFrame>;
  /** §5.2 第 3 条：**用 `values` 重载持久化的线程状态**。这是 §3.1 那个例外 */
  reloadDurableState: () => Promise<void>;
  onRecovery?: (event: StreamRecoveryEvent) => void;
  /** 第 1 条：清掉过期的重连元数据（`Last-Event-ID`） */
  onResetCursor?: (frame: StreamGapFrame) => void;
  maxRecoveries?: number;
}

/** 一个把 gap 转成"续订"的信号。非 gap 帧原样透传 */
function isEndFrame(frame: unknown): boolean {
  return (
    typeof frame === "object" &&
    frame !== null &&
    (frame as { event?: unknown }).event === "end"
  );
}

/**
 * 包住一条流：遇到 gap 就重载 + 续订，而不是把 gap 当成流结束。
 *
 * 保持**惰性异步可迭代**，因为 SDK 是用 `for await` 消费它的。
 */
export async function* withGapRecovery<TFrame>(
  source: AsyncIterable<TFrame>,
  options: GapRecoveryOptions<TFrame>,
): AsyncIterable<TFrame> {
  const maxAttempts = options.maxRecoveries ?? DEFAULT_MAX_RECOVERIES;
  let current = source;
  let attempt = 0;

  for (;;) {
    let gap: StreamGapFrame | null = null;

    for await (const frame of current) {
      const detected = options.classify(frame);
      if (detected) {
        gap = detected;
        break; // 跳出会触发底层迭代器的 return()，订阅立即停止
      }
      yield frame;
      if (isEndFrame(frame)) return; // 正常结束
    }

    if (!gap) return; // 上游自然关闭，不是 gap

    attempt += 1;
    if (attempt > maxAttempts) {
      const event: StreamRecoveryEvent = {
        frame: gap,
        attempt,
        maxAttempts,
        exhausted: true,
      };
      options.onRecovery?.(event);
      // **绝不能把 gap 表现为一次正常的流结束**：抛错，让上层显示"恢复失败"，
      // 而不是"完成了"——后端的 run 还在跑，显示完成会让用户以为回答就这么短。
      throw new StreamRecoveryExhaustedError(gap, maxAttempts);
    }

    // 顺序按 §5.2：清游标 → 通知上层 → 重载状态 → 续订
    options.onResetCursor?.(gap);
    options.onRecovery?.({ frame: gap, attempt, maxAttempts, exhausted: false });
    await options.reloadDurableState();
    current = options.resubscribe(nextSubscriptionCursor(gap));
  }
}
