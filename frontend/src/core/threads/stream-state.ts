import type {
  SandboxState,
  ThreadState,
  TodoItem,
  ViewedImageData,
} from "../types";

/**
 * 从 `updates` 帧里折叠非消息字段（M5 §3.2）。
 *
 * 状态归属的划分：
 *   - SDK 的 `messages-tuple` 管理器 → 实时消息块的组装和去重。**唯一所有者**
 *   - 这里 → artifacts、todos、目标状态……
 *
 * 硬要求：**折叠时要用和后端 reducer 一致的语义**（M1 §2.2）。
 * 两边不一致，前后端会显示出不同的状态。
 *
 * 无关的帧要在调用 SDK 的状态改写器**之前**就拒掉——见 `isRelevantUpdateFrame`。
 */

/** M1 的字段登记表。带 reducer 的通道，可能在同一 super-step 里被多处写入 */
export const THREAD_STATE_REDUCER_FIELDS = [
  "messages",
  "sandbox",
  "artifacts",
  "todos",
  "viewed_images",
  "delegations",
  "skill_context",
  "promoted",
] as const;

/** 走 LangGraph 默认 LastValue 通道的裸字段（单一 writer） */
export const THREAD_STATE_PLAIN_FIELDS = [
  "thread_data",
  "title",
  "uploaded_files",
  "summary_text",
] as const;

export const THREAD_STATE_FIELDS: ReadonlySet<string> = new Set([
  ...THREAD_STATE_REDUCER_FIELDS,
  ...THREAD_STATE_PLAIN_FIELDS,
]);

export class StateReducerConflictError extends Error {
  constructor(
    readonly field: string,
    readonly existing: unknown,
    readonly incoming: unknown,
  ) {
    super(
      `Reducer conflict on "${field}": existing=${JSON.stringify(existing)} ` +
        `incoming=${JSON.stringify(incoming)}`,
    );
    this.name = "StateReducerConflictError";
  }
}

const isPlainObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** 累加去重：用 `dict.fromkeys` 去重且保序（M1 §2.2） */
export function mergeArtifacts(
  existing: readonly string[] | undefined,
  incoming: unknown,
): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const value of [...(existing ?? []), ...(Array.isArray(incoming) ? incoming : [])]) {
    if (typeof value !== "string" || seen.has(value)) continue;
    seen.add(value);
    out.push(value);
  }
  return out;
}

/**
 * 幂等写，冲突即 bug（**fail closed**）。
 *
 * 多个沙箱工具可能在同一个 graph step 里懒初始化，发出相同的 sandbox_id。
 * 但同一 thread 里出现不同的 sandbox_id 说明生命周期/隔离有 bug，
 * 所以宁可失败也不要静默选一个。
 */
export function mergeSandbox(
  existing: SandboxState | null | undefined,
  incoming: unknown,
): SandboxState | null {
  if (incoming === null || incoming === undefined) return existing ?? null;
  if (!isPlainObject(incoming)) {
    throw new StateReducerConflictError("sandbox", existing ?? null, incoming);
  }
  const next = incoming as SandboxState;
  if (existing == null) return next;
  if (existing.sandbox_id !== next.sandbox_id) {
    throw new StateReducerConflictError("sandbox", existing, next);
  }
  return { ...existing, ...next };
}

/**
 * 最后非 None 获胜。
 * `None` = 没动，`[]` = 显式清空，两者不同（M1 §2.2）。
 */
export function mergeTodos(
  existing: readonly TodoItem[] | null | undefined,
  incoming: unknown,
): TodoItem[] | null {
  if (incoming === undefined || incoming === null) return existing ? [...existing] : null;
  if (!Array.isArray(incoming)) return existing ? [...existing] : null;
  return incoming as TodoItem[];
}

/** 合并 + 空字典清空：`{}` 是"清空"信号，不是"没动" */
export function mergeViewedImages(
  existing: Readonly<Record<string, ViewedImageData>> | undefined,
  incoming: unknown,
): Record<string, ViewedImageData> {
  if (!isPlainObject(incoming)) return { ...(existing ?? {}) };
  if (Object.keys(incoming).length === 0) return {};
  return { ...(existing ?? {}), ...(incoming as Record<string, ViewedImageData>) };
}

/** 累加去重：`promoted` 和 `artifacts` 同一套语义 */
export const mergePromoted = mergeArtifacts;

function isNonEmptyUpdate(value: unknown): boolean {
  if (value === undefined) return false;
  if (value === null) return true;
  if (Array.isArray(value)) return value.length > 0;
  if (isPlainObject(value)) return true;
  return true;
}

/**
 * 无关的帧要在调用 SDK 的状态改写器**之前**就拒掉。
 *
 * `updates` 帧的形状是 `{ [nodeName]: partialState }`。一个帧只要它带的
 * 任何一个 key 落在 `ThreadState` 里就算相关；都不在就直接丢，不进折叠。
 */
export function isRelevantUpdateFrame(chunk: unknown): boolean {
  if (!isPlainObject(chunk)) return false;
  const candidates: unknown[] = [chunk];
  for (const value of Object.values(chunk)) {
    if (isPlainObject(value)) candidates.push(value);
  }
  return candidates.some((candidate) =>
    Object.keys(candidate as Record<string, unknown>).some((key) => THREAD_STATE_FIELDS.has(key)),
  );
}

/**
 * 折叠一个 `updates` 帧。
 *
 * `messages` 通道**故意不在这里折**——它的唯一所有者是 SDK 的 messages-tuple
 * 管理器（§3.2）。这里只处理非消息字段。
 */
export function foldUpdateFrame(state: ThreadState, chunk: unknown): ThreadState {
  if (!isRelevantUpdateFrame(chunk)) return state;

  const next: ThreadState = { ...state };
  const patches: Record<string, unknown>[] = [chunk as Record<string, unknown>];
  for (const value of Object.values(chunk as Record<string, unknown>)) {
    if (isPlainObject(value)) patches.push(value);
  }

  for (const patch of patches) {
    for (const [key, value] of Object.entries(patch)) {
      switch (key) {
        case "artifacts":
          next.artifacts = mergeArtifacts(next.artifacts, value);
          break;
        case "promoted":
          next.promoted = mergePromoted(next.promoted, value);
          break;
        case "sandbox":
          next.sandbox = mergeSandbox(next.sandbox, value);
          break;
        case "todos":
          next.todos = mergeTodos(next.todos, value);
          break;
        case "viewed_images":
          next.viewed_images = mergeViewedImages(next.viewed_images, value);
          break;
        case "delegations":
          next.delegations = mergeArtifacts(next.delegations as never, value) as never;
          break;
        case "skill_context":
          next.skill_context = mergeArtifacts(next.skill_context as never, value) as never;
          break;
        case "thread_data":
        case "title":
        case "uploaded_files":
        case "summary_text":
          if (isNonEmptyUpdate(value)) {
            (next as Record<string, unknown>)[key] = value;
          }
          break;
        default:
          // 无关的 key 在 isRelevantUpdateFrame 之后不会再出现，
          // 但真出现了也**不能**顺手塞进 state：那正是"前后端状态不一致"的来源
          break;
      }
    }
  }

  return next;
}

export function createEmptyThreadState(): ThreadState {
  return {
    messages: [],
    sandbox: null,
    artifacts: [],
    todos: null,
    viewed_images: {},
    delegations: [],
    skill_context: [],
    promoted: [],
    thread_data: null,
    title: null,
    uploaded_files: null,
    summary_text: null,
  };
}
