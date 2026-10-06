import type { ChatMessage } from "../types";
import { messageKey } from "../messages/identity";
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
