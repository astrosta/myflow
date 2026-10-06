import type { StreamGapFrame } from "../types";
import {
  DEFAULT_MAX_RECOVERIES,
  StreamRecoveryExhaustedError,
  asGapFrame,
  formatSseFrame,
  parseSseStream,
  withGapRecovery,
  type StreamRecoveryEvent,
} from "./gap";

/**
 * 把 gap 恢复接进 `fetch`（M5 §5.2）。
 *
 * 为什么在这一层：**上游 SDK 会忽略它不认识的事件名**。
 * 包在 SDK 解析后的输出上，gap 已经被丢掉了；只有包在 `Response` 的 body 上
 * 才能在 SDK 之前看到 `event: gap`，并且把续订的流无缝接进同一个 body——
 * 这样 SDK 侧看起来就是"同一条流继续在走"，而不是"流结束了"。
 */

export interface RecoveryContext {
  /**
   * §5.2 第 3 条：用 `values` 重载持久化的线程状态。
   *
   * 入参是**从请求 URL 解析出来的** thread_id，而不是注册时的那一个：
   * 新会话的 thread 是 SDK 在第一次 submit 时才建的，注册的那一刻还不存在。
   */
  reloadDurableState: (threadId: string) => Promise<void>;
  /** §5.2 第 2 条：发一个内部事件通知上层 */
  onRecovery?: (event: StreamRecoveryEvent) => void;
  /** §5.2 第 1 条：清掉过期的重连元数据 */
  onResetCursor?: (frame: StreamGapFrame) => void;
  maxRecoveries?: number;
}

/**
 * 按 thread 注册恢复上下文。客户端保持单例，恢复语义按会话注入。
 */
const registry = new Map<string, RecoveryContext>();

/**
 * 通配键：注册时还不知道 thread_id 的场合用（新会话首屏）。
 *
 * 精确注册永远优先——通配只是让"第一次发送到 thread 建好"这段窗口里
 * 发生 gap 时也能恢复，而不是静默退化成"流正常结束"。
 */
export const WILDCARD_THREAD = "*";

export function registerRecoveryContext(threadId: string, context: RecoveryContext): () => void {
  registry.set(threadId, context);
  return () => {
    if (registry.get(threadId) === context) registry.delete(threadId);
  };
}

function lookupRecoveryContext(threadId: string): RecoveryContext | undefined {
  return registry.get(threadId) ?? registry.get(WILDCARD_THREAD);
}

export function threadIdFromUrl(url: string): string | null {
  const match = /\/threads\/([^/?#]+)/.exec(url);
  return match ? decodeURIComponent(match[1]!) : null;
}

/**
 * 续订用的 join 路径。
 *
 * `Content-Location: /api/threads/{tid}/runs/{run_id}`（M3 §8.1）是**必需的**，
 * 不是可选优化：SDK 的 `onCreated` 也要从它里面抠 `run_id`。
 *
 * 这里刻意**只做字符串拼接，不解析 URL**——请求 URL 在浏览器里常常是相对的
 * （`apiUrl` 是 `/api`），`new URL(relative)` 会直接抛，
 * 那会把"能恢复"变成"恢复失败"。
 */
export function resolveJoinPath(requestUrl: string, contentLocation: string | null): string | null {
  if (contentLocation) {
    const path = contentLocation.startsWith("http")
      ? new URL(contentLocation).pathname
      : contentLocation;
    const trimmed = path.replace(/\/$/, "");
    if (trimmed) return `${trimmed}/join`;
  }
  // 退路：请求 URL 本身就是 `/runs/{run_id}/stream`
  const match = /^(.*\/runs\/[^/?#]+)\/stream\/?(\?.*)?$/.exec(requestUrl);
  return match ? `${match[1]}/join${match[2] ?? ""}` : null;
}

function isEventStream(response: Response): boolean {
  const contentType = response.headers.get("content-type") ?? "";
  return contentType.includes("text/event-stream") && Boolean(response.body);
}

async function* bodyFrames(body: ReadableStream<Uint8Array>): AsyncIterable<Uint8Array> {
  const reader = body.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) return;
      if (value) yield value;
    }
  } finally {
    reader.releaseLock();
  }
}

export interface GapRecoveringFetchOptions {
  baseFetch?: typeof fetch;
  /** 默认 5 次（§5.2 第 5 条） */
  maxRecoveries?: number;
}

export function createGapRecoveringFetch(options: GapRecoveringFetchOptions = {}): typeof fetch {
  const baseFetch = options.baseFetch ?? globalThis.fetch.bind(globalThis);

  const wrapped = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const response = await baseFetch(input, init);
    if (!isEventStream(response)) return response;

    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const threadId = threadIdFromUrl(url);
    const context = threadId ? lookupRecoveryContext(threadId) : undefined;
    // 没有注册恢复语义（比如非聊天页发的流）就原样透传，别自作主张
    if (!threadId || !context || !response.body) return response;

    const joinPath = resolveJoinPath(url, response.headers.get("content-location"));
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      async start(controller) {
        const openJoin = async (cursor: string | null): Promise<ReadableStream<Uint8Array>> => {
          if (!joinPath) {
            throw new Error(
              "cannot recover SSE stream: response has no Content-Location to derive the join URL from",
            );
          }
          const headers = new Headers(init?.headers);
          // 缓冲区空了就不带游标重新 join（§5.2 第 4 条）
          if (cursor) headers.set("Last-Event-ID", cursor);
          else headers.delete("Last-Event-ID");
          /**
           * **必须把 body 清掉。** 原始请求是 `POST /runs/stream`，它的 init 里带着
           * body；直接展开再加 `method: "GET"` 会让浏览器抛
           * `Request with GET/HEAD method cannot have body` —— 续订请求根本发不出去，
           * gap 恢复静默失败（表现是网络一抖这一轮就死在那儿）。
           *
           * join 是 GET，参数只走 Last-Event-ID 头。
           */
          const joined = await baseFetch(joinPath, {
            ...init,
            method: "GET",
            body: undefined,
            headers,
          });
          if (!joined.body) throw new Error("join stream has no body");
          return joined.body;
        };

        const source = withGapRecovery(parseSseStream(bodyFrames(response.body!)), {
          classify: asGapFrame,
          resubscribe: async function* (cursor) {
            yield* parseSseStream(bodyFrames(await openJoin(cursor)));
          },
          reloadDurableState: () => context.reloadDurableState(threadId),
          ...(context.onRecovery ? { onRecovery: context.onRecovery } : {}),
          ...(context.onResetCursor ? { onResetCursor: context.onResetCursor } : {}),
          maxRecoveries: context.maxRecoveries ?? options.maxRecoveries ?? DEFAULT_MAX_RECOVERIES,
        });

        try {
          for await (const frame of source) {
            controller.enqueue(encoder.encode(formatSseFrame(frame)));
          }
          controller.close();
        } catch (error) {
          /**
           * 只有**恢复次数用尽**才自己造一帧 error 然后收尾——
           * §5.2 明确要求"绝不能让 gap 表现为一次正常的流结束"。
           *
           * 其它错误（连接被掐断、socket 异常）**必须原样抛回去**：
           * SDK 自己有一套重连逻辑（maxReconnectAttempts），
           * 在这里吞掉并补一帧 error 会让它以为流正常结束了，
           * 于是永远不重连——表现是网络抖一下对话就死在那儿。
           */
          if (error instanceof StreamRecoveryExhaustedError) {
            controller.enqueue(
              encoder.encode(
                formatSseFrame({
                  event: "error",
                  data: JSON.stringify({
                    error: "stream_recovery_exhausted",
                    message: "SSE replay gap could not be recovered",
                  }),
                  id: null,
                }),
              ),
            );
            controller.close();
            return;
          }
          controller.error(error);
        }
      },
    });

    return new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  };

  return wrapped as typeof fetch;
}
