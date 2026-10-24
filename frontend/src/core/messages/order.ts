import type { ChatMessage, ToolCall } from "../types";
import { messageKey } from "./identity";

/**
 * 把"持久化的历史"和"实时的流"合并成一条正确排序的对话（M5 §4）。
 *
 * 位置权威：**服务端 seq > 身份锚点编织 > 到达顺序**。
 *
 * 屏幕上的三类消息：
 *   1. 历史页里的（带服务端全局 seq）
 *   2. 实时流里的（还没进 feed，没有 seq）
 *   3. checkpoint 快照里的（run 开始时的状态，可能和 1 有重叠 id）
 *
 * M6 的压缩会把第 3 类变成"只有当前 run 的尾巴"，和第 1 类可能一条 id 都对不上。
 * 那个场景就是下面 `belowFloor` 分支存在的全部理由。
 */

export interface TurnBaseline {
  /** 归一化后的人类消息身份：本轮提交的那条 */
  humanKey: string;
  /** 本轮开始前已经存在的身份集合。只有**非基线**的可见步骤会被移动 */
  keys: ReadonlySet<string>;
  /** 本地提交的 run_id。join 回来的 run 不属于本轮，不能动（§4.4） */
  localRunId?: string | null;
}

export interface MergeInput {
  /** 已加载的历史页（升序，可能不连续） */
  history?: readonly ChatMessage[];
  /** messages-tuple 管理器给的实时消息。可能和历史是同一条的不同副本 */
  stream?: readonly ChatMessage[];
  /** 用户刚发出、服务端还没确认的（§4.4：永远在最后） */
  optimistic?: readonly ChatMessage[];
  /**
   * 当前已加载窗口的下界 seq。
   * `null` 表示窗口已经触到会话开头（或什么都没加载），此时不存在"窗口下界之下"。
   */
  loadedFloorSeq?: number | null;
  /** 本地提交那一轮的身份基线（§4.4） */
  turnBaseline?: TurnBaseline | null;
}

export interface MergeDiagnostics {
  /** 低于窗口下界、被置顶救回来的条数，以及它们的 seq */
  prependedBelowFloor: number[];
  /** 同身份被折叠掉的副本数 */
  collapsedCopies: number;
  /** 没有 seq、靠到达顺序编织的条数 */
  wovenWithoutSeq: number;
  /** 被身份基线移动的步骤数 */
  baselineStepsMoved: number;
}

interface Entry {
  key: string;
  message: ChatMessage;
  firstArrival: number;
  lastArrival: number;
  seq: number | null;
}

const isNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

function firstDefined<T>(values: readonly (T | null | undefined)[]): T | undefined {
  for (const value of values) if (value !== null && value !== undefined) return value;
  return undefined;
}

export function contentLength(message: ChatMessage): number {
  const { content } = message;
  if (typeof content === "string") return content.length;
  if (!Array.isArray(content)) return 0;
  return content.reduce((total, block) => {
    const text = typeof block?.text === "string" ? block.text : "";
    return total + text.length;
  }, 0);
}

function mergeToolCalls(copies: readonly ChatMessage[]): ToolCall[] {
  const byId = new Map<string, ToolCall>();
  const anonymous: ToolCall[] = [];
  for (const copy of copies) {
    for (const call of copy.tool_calls ?? []) {
      const id = call.id;
      if (!id) {
        anonymous.push(call);
        continue;
      }
      const previous = byId.get(id);
      // 后到的副本更完整（流式的 args 是拼出来的），但别把已知字段丢成 undefined
      byId.set(id, previous ? { ...previous, ...dropUndefined(call) } : call);
    }
  }
  return [...byId.values(), ...anonymous];
}

function dropUndefined(call: ToolCall): ToolCall {
  const out: ToolCall = {};
  if (call.id !== undefined) out.id = call.id;
  if (call.name !== undefined) out.name = call.name;
  if (call.args !== undefined) out.args = call.args;
  if (call.argsText !== undefined) out.argsText = call.argsText;
  return out;
}

/**
 * 同一个身份出现多次时，**最早的那个 seq 获胜**。
 * 隐藏的控制副本只在没有可见副本携带位置时才贡献位置（§4.2）。
 */
function pickSeq(copies: readonly ChatMessage[]): number | null {
  const visible = copies.filter((copy) => !copy.hidden);
  for (const pool of [visible, copies]) {
    const seqs = pool.map((copy) => copy.seq).filter(isNumber);
    if (seqs.length) return Math.min(...seqs);
  }
  return null;
}

/**
 * 折叠同身份的多个副本。
 *
 * §4.4：**内容替换不能丢掉已知的 `seq`、`run_id` 和运行时长。**
 * 流式追加会让后面的副本内容更长，但元数据往往只在前面的副本上。
 */
function mergeCopies(copies: readonly ChatMessage[], seq: number | null): ChatMessage {
  const visible = copies.filter((copy) => !copy.hidden);
  const pool = visible.length ? visible : copies;

  let content = pool[0]!.content;
  let bestLength = -1;
  for (const copy of pool) {
    const length = contentLength(copy);
    if (length >= bestLength) {
      bestLength = length;
      content = copy.content;
    }
  }

  const toolCalls = mergeToolCalls(pool);
  const base = pool[pool.length - 1]!;
  const message: ChatMessage = {
    ...base,
    content,
    hidden: visible.length === 0,
    optimistic: copies.every((copy) => copy.optimistic === true),
  };
  if (toolCalls.length) message.tool_calls = toolCalls;
  else delete message.tool_calls;

  const resolvedSeq = seq ?? firstDefined(copies.map((copy) => copy.seq));
  if (isNumber(resolvedSeq)) message.seq = resolvedSeq;
  const runId = firstDefined(copies.map((copy) => copy.run_id));
  if (runId !== undefined) message.run_id = runId;
  const createdAt = firstDefined(copies.map((copy) => copy.created_at));
  if (createdAt !== undefined) message.created_at = createdAt;
  const duration = firstDefined(copies.map((copy) => copy.duration_ms));
  if (duration !== undefined) message.duration_ms = duration;

  return message;
}

/**
 * §4.4：一条消息的内容被更新时（流式追加、压缩后重建），元数据要保留。
 * 用在"服务端给了更权威的正文，但没带 seq/run_id/时长"的场合。
 */
export function replaceMessageContent(
  previous: ChatMessage | undefined,
  next: ChatMessage,
): ChatMessage {
  if (!previous) return next;
  const merged: ChatMessage = { ...next };
  if (!isNumber(merged.seq) && isNumber(previous.seq)) merged.seq = previous.seq;
  if (merged.run_id == null && previous.run_id != null) merged.run_id = previous.run_id;
  if (merged.duration_ms == null && previous.duration_ms != null) {
    merged.duration_ms = previous.duration_ms;
  }
  if (merged.created_at == null && previous.created_at != null) {
    merged.created_at = previous.created_at;
  }
  return merged;
}

export function mergeMessagesWithDiagnostics(input: MergeInput): {
  messages: ChatMessage[];
  diagnostics: MergeDiagnostics;
} {
  const {
    history = [],
    stream = [],
    optimistic = [],
    loadedFloorSeq = null,
    turnBaseline = null,
  } = input;

  const arrival = [...history, ...stream];
  const buckets = new Map<string, ChatMessage[]>();
  const byKey = new Map<string, Entry>();
  const entries: Entry[] = [];

  arrival.forEach((message, index) => {
    const key = messageKey(message);
    const bucket = buckets.get(key);
    const existing = byKey.get(key);
    if (bucket && existing) {
      bucket.push(message);
      existing.lastArrival = index;
      return;
    }
    const entry: Entry = {
      key,
      message,
      firstArrival: index,
      lastArrival: index,
      seq: null,
    };
    buckets.set(key, [message]);
    byKey.set(key, entry);
    entries.push(entry);
  });

  let collapsedCopies = 0;
  const positioned: Entry[] = [];
  const unanchored: Entry[] = [];
  const belowFloor: Entry[] = [];

  for (const entry of entries) {
    const copies = buckets.get(entry.key)!;
    collapsedCopies += copies.length - 1;
    const seq = pickSeq(copies);
    entry.seq = seq;
    entry.message = mergeCopies(copies, seq);
    if (seq === null) unanchored.push(entry);
    else if (loadedFloorSeq !== null && seq < loadedFloorSeq) belowFloor.push(entry);
    else positioned.push(entry);
  }

  positioned.sort((a, b) => (a.seq! - b.seq!) || (a.firstArrival - b.firstArrival));
  belowFloor.sort((a, b) => (a.seq! - b.seq!) || (a.firstArrival - b.firstArrival));
  unanchored.sort((a, b) => a.firstArrival - b.firstArrival);

  // ---- 身份锚点编织：没有 seq 的片段插在"它到达前最后一个有位置的邻居"之后 ----
  const frontWoven: Entry[] = [];
  const insertAfter = new Map<string, Entry[]>();
  let segment: Entry[] = [];

  const flush = () => {
    if (!segment.length) return;
    const segmentStart = segment[0]!.firstArrival;
    let anchor: Entry | undefined;
    for (const candidate of positioned) {
      if (candidate.lastArrival < segmentStart) {
        if (!anchor || candidate.lastArrival > anchor.lastArrival) anchor = candidate;
      }
    }
    if (!anchor) frontWoven.push(...segment);
    else {
      const bucket = insertAfter.get(anchor.key);
      if (bucket) bucket.push(...segment);
      else insertAfter.set(anchor.key, [...segment]);
    }
    segment = [];
  };

  for (const entry of unanchored) {
    const previous = segment[segment.length - 1];
    // 片段 = 到达顺序上连续的一段无 seq 消息；中间插入了有位置的消息就断开
    if (previous && entry.firstArrival !== previous.lastArrival + 1) flush();
    segment.push(entry);
  }
  flush();

  const ordered: ChatMessage[] = [];
  for (const entry of belowFloor) ordered.push(entry.message);
  for (const entry of frontWoven) ordered.push(entry.message);
  for (const entry of positioned) {
    ordered.push(entry.message);
    const woven = insertAfter.get(entry.key);
    if (woven) for (const item of woven) ordered.push(item.message);
  }

  const baselineStepsMoved = applyTurnBaseline(ordered, turnBaseline);

  // 乐观消息永远在最后，但**不是无条件追加**：服务端确认之后，
  // 同一条消息已经在上面了（身份归一化会把提交帧 X 和 checkpoint 的 X__user 认成同一条），
  // 再追加一次会让屏幕上出现两条一模一样的提问。
  const known = new Set(ordered.map((message) => messageKey(message)));
  for (const message of optimistic) {
    const key = messageKey(message);
    if (known.has(key)) continue;
    known.add(key);
    const insertAt = optimisticInsertIndex(ordered, turnBaseline, key);
    if (insertAt === null) ordered.push(message);
    else ordered.splice(insertAt, 0, message);
  }

  return {
    messages: ordered,
    diagnostics: {
      prependedBelowFloor: belowFloor.map((entry) => entry.seq!),
      collapsedCopies,
      wovenWithoutSeq: unanchored.length,
      baselineStepsMoved,
    },
  };
}

/**
 * 乐观消息该插在哪。`null` = 追加到末尾。
 *
 * 只有"这一条就是本轮提交的那条提问、而它的规范副本还没到"时才往前插：
 * 插在本轮**非基线步骤**的第一条之前，这样思考和工具调用永远在提问下面。
 */
function optimisticInsertIndex(
  ordered: readonly ChatMessage[],
  baseline: TurnBaseline | null | undefined,
  key: string,
): number | null {
  if (!baseline || key !== baseline.humanKey) return null;
  if (ordered.some((message) => messageKey(message) === key)) return null;

  for (let index = 0; index < ordered.length; index += 1) {
    const message = ordered[index]!;
    if (message.type !== "ai" && message.type !== "tool") continue;
    if (message.hidden) continue;
    if (baseline.keys.has(messageKey(message))) continue;
    const sameRun =
      baseline.localRunId == null ||
      message.run_id == null ||
      message.run_id === baseline.localRunId;
    if (!sameRun) continue;
    return index;
  }
  return null;
}

/**
 * §4.4：如果流在规范历史发布用户消息之前就发布了新的 AI/工具步骤，
 * 只把**非基线**的可见步骤移到新的人类消息之后，不要动历史、隐藏控制消息和重连的 run。
 */
function applyTurnBaseline(
  ordered: ChatMessage[],
  baseline: TurnBaseline | null | undefined,
): number {
  if (!baseline) return 0;
  const anchorIndex = ordered.findIndex((message) => messageKey(message) === baseline.humanKey);
  if (anchorIndex < 0) return 0;

  const movable: ChatMessage[] = [];
  const kept: ChatMessage[] = [];
  ordered.forEach((message, index) => {
    const isStep = message.type === "ai" || message.type === "tool";
    const isSameRun =
      baseline.localRunId == null ||
      message.run_id == null ||
      message.run_id === baseline.localRunId;
    const move =
      index < anchorIndex &&
      isStep &&
      !message.hidden &&
      !message.optimistic &&
      isSameRun &&
      !baseline.keys.has(messageKey(message));
    if (move) movable.push(message);
    else kept.push(message);
  });

  if (!movable.length) return 0;

  const insertAt = kept.findIndex((message) => messageKey(message) === baseline.humanKey);
  ordered.length = 0;
  ordered.push(...kept.slice(0, insertAt + 1), ...movable, ...kept.slice(insertAt + 1));
  return movable.length;
}

export function mergeMessages(input: MergeInput): ChatMessage[] {
  return mergeMessagesWithDiagnostics(input).messages;
}

/**
 * 已加载窗口的下界。`hasMoreBefore=false` 表示已经翻到会话开头，
 * 此时不存在"窗口下界之下"——任何更旧的 seq 都不该被置顶。
 */
export function computeLoadedFloorSeq(
  pages: readonly (readonly ChatMessage[])[],
  hasMoreBefore: boolean,
): number | null {
  if (hasMoreBefore) {
    const seqs = pages.flat().map((message) => message.seq).filter(isNumber);
    return seqs.length ? Math.min(...seqs) : null;
  }
  return null;
}
