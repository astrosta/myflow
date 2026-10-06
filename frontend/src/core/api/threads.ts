import { apiUrl } from "./base";

/**
 * Thread 的读写。
 *
 * **不用 SDK 的 client.threads.*。** 网关的请求模型是 extra="forbid"，
 * 而 SDK 会按 LangGraph Platform 的形状发 `offset` / `sortBy` / `sortOrder`，
 * 实测每次都 422：
 *
 *   {"loc":["body","offset"],"msg":"Extra inputs are not permitted"}
 *
 * 网关真正接受的是（router-layer 文档 §路由表 / models/request.py）：
 *   POST /api/threads/search  { metadata?, status?, limit?, cursor?, order? }
 *   GET  /api/threads         ?limit=&cursor=&order=
 *
 * 所以这一层直接对齐网关，不经过 SDK。
 */

export interface ThreadRow {
  thread_id: string;
  incarnation: string | null;
  assistant_id: string | null;
  display_name: string | null;
  status: string;
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

export interface Page<T> {
  data: T[];
  next_cursor: string | null;
  has_more: boolean;
}

/** 后端 ThreadId 的约束：^[A-Za-z0-9_-]{1,64}$ */
export const THREAD_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function parsePage<T>(payload: unknown, what: string): Page<T> {
  if (!isRecord(payload) || !Array.isArray(payload.data)) {
    throw new Error("Unexpected " + what + " payload: expected { data: [...] }");
  }
  return {
    data: payload.data as T[],
    next_cursor: typeof payload.next_cursor === "string" ? payload.next_cursor : null,
    has_more: payload.has_more === true,
  };
}

export interface ListThreadsOptions {
  limit?: number;
  cursor?: string | null;
  order?: "asc" | "desc";
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

export async function listThreads(options: ListThreadsOptions = {}): Promise<Page<ThreadRow>> {
  const { limit = 50, cursor = null, order = "desc", signal, fetchImpl = fetch } = options;
  const params = new URLSearchParams({ limit: String(limit), order });
  if (cursor) params.set("cursor", cursor);

  const response = await fetchImpl(apiUrl("/threads?" + params.toString()), { signal });
  if (!response.ok) {
    throw new Error("Failed to list threads: HTTP " + response.status);
  }
  return parsePage<ThreadRow>(await response.json(), "/threads");
}

export interface CreateThreadOptions {
  /** 客户端可以先定 id（后端 ThreadCreateRequest.thread_id 允许） */
  threadId?: string | null;
  assistantId?: string | null;
  metadata?: Record<string, unknown> | null;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

export async function createThread(options: CreateThreadOptions = {}): Promise<ThreadRow> {
  const { threadId = null, assistantId = null, metadata = null, signal, fetchImpl = fetch } = options;
  const body: Record<string, unknown> = {};
  if (threadId) body.thread_id = threadId;
  if (assistantId) body.assistant_id = assistantId;
  if (metadata) body.metadata = metadata;

  const response = await fetchImpl(apiUrl("/threads"), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    throw new Error("Failed to create thread: HTTP " + response.status);
  }
  return (await response.json()) as ThreadRow;
}

export interface ThreadStateSnapshot {
  values: Record<string, unknown>;
  checkpoint_id: string | null;
  checkpoint_ns: string | null;
}

/**
 * 读 checkpoint。
 *
 * 同样不用 SDK：网关返回的是 { values, checkpoint_id, checkpoint_ns }，
 * 而 SDK 的 getState 期望 LangGraph Platform 那套
 * { values, next, checkpoint, metadata, tasks }，形状对不上。
 */
export async function getThreadState(
  threadId: string,
  options: { signal?: AbortSignal; fetchImpl?: typeof fetch } = {},
): Promise<ThreadStateSnapshot> {
  const { signal, fetchImpl = fetch } = options;
  const response = await fetchImpl(
    apiUrl("/threads/" + encodeURIComponent(threadId) + "/state"),
    { signal },
  );
  if (!response.ok) {
    throw new Error("Failed to read state for thread " + threadId + ": HTTP " + response.status);
  }
  const payload = (await response.json()) as Partial<ThreadStateSnapshot>;
  return {
    values: isRecord(payload.values) ? payload.values : {},
    checkpoint_id: typeof payload.checkpoint_id === "string" ? payload.checkpoint_id : null,
    checkpoint_ns: typeof payload.checkpoint_ns === "string" ? payload.checkpoint_ns : null,
  };
}
