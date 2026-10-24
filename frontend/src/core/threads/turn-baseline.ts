import type { ChatMessage } from "../types";
import { messageKey } from "../messages/identity";
import { textOf } from "../messages/tool-results";
import type { TurnBaseline } from "../messages/order";

/**
 * 本地提交那一轮要记一个身份基线（M5 §4.4）。
 *
 * 基线 = "这一轮开始前屏幕上已经存在的所有身份"。有了它，
 * 才分得清"流新发布的步骤"和"本来就在那儿的步骤"——
 * 后者不能被移动，前者必须被移动到新的人类消息之后。
 */
export function createTurnBaseline(
  existing: readonly ChatMessage[],
  humanKey: string,
  localRunId: string | null,
): TurnBaseline {
  return {
    humanKey,
    keys: new Set(existing.map((message) => messageKey(message))),
    localRunId,
  };
}

let localSequence = 0;

/** 乐观消息的 id。服务端确认后会换成规范 id，靠身份归一化对齐 */
export function newOptimisticId(): string {
  localSequence += 1;
  return `local-${Date.now().toString(36)}-${localSequence.toString(36)}`;
}

export interface OptimisticInput {
  text: string;
  id?: string;
  createdAt?: string;
}

/**
 * 用户刚发出、服务端还没确认的那条。
 * §4.4：**乐观消息永远在最后**——排序由 `mergeMessages` 保证，
 * 这里只负责把它标成 `optimistic`。
 */
export function createOptimisticHumanMessage({
  text,
  id,
  createdAt,
}: OptimisticInput): ChatMessage {
  return {
    id: id ?? newOptimisticId(),
    type: "human",
    content: text,
    seq: null,
    run_id: null,
    created_at: createdAt ?? new Date().toISOString(),
    optimistic: true,
  };
}

/**
 * 乐观消息的最终处置（提交后每次渲染都重算）：
 *
 * - `pending`：服务端还没任何反应——继续显示"发送中…"；
 * - 服务端已经把这条提问发回来了（**可能换了 id**，所以按正文比）：去掉乐观副本，
 *   否则屏幕上会出现两条一模一样的提问；
 * - 已经开始（步骤都来了）但还没回显：保留这条，只是**摘掉"发送中"**——
 *   问题不能在这一轮里凭空消失。
 */
export interface OptimisticState {
  messages: ChatMessage[];
  pending: boolean;
}

export function reconcileOptimistic(
  optimistic: readonly ChatMessage[],
  stream: readonly ChatMessage[],
  baseline: TurnBaseline | null | undefined,
): OptimisticState {
  if (!optimistic.length) return { messages: [], pending: false };

  const baselineKeys = baseline?.keys;
  const fresh = stream.filter(
    (message) => !message.hidden && !baselineKeys?.has(messageKey(message)),
  );
  const started = fresh.length > 0;

  const echoes = fresh.filter((message) => message.type === "human").map((message) => textOf(message).trim());
  const echoed = optimistic.every((message) => echoes.includes(textOf(message).trim()));
  if (echoed) return { messages: [], pending: false };

  return {
    messages: started
      ? optimistic.map((message) => ({ ...message, optimistic: false }))
      : [...optimistic],
    pending: !started,
  };
}

/**
 * 这一轮是否已经"看得见地开始了"。
 *
 * 判据：流里出现了**不在基线里**的可见消息——人类消息算，AI/工具步骤也算。
 * 只看人类消息是不够的：服务端可能先发布步骤、再补那条提问（或者干脆换了 id），
 * 于是乐观消息会一直挂着"发送中…"。
 */
export function hasNewTurnMessages(
  stream: readonly ChatMessage[],
  baseline: TurnBaseline | null | undefined,
): boolean {
  const baselineKeys = baseline?.keys;
  return stream.some(
    (message) => !message.hidden && !baselineKeys?.has(messageKey(message)),
  );
}

/**
 * 服务端确认了本地提交之后，把乐观消息换成规范副本。
 * §4.4：内容替换不能丢掉已知的 `seq`、`run_id` 和运行时长。
 */
export function isOptimisticConfirmed(
  optimistic: ChatMessage,
  confirmed: readonly ChatMessage[],
): boolean {
  const key = messageKey(optimistic);
  return confirmed.some((message) => messageKey(message) === key);
}
