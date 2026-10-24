"use client";

import type { AnswerGroup } from "@/core/messages/group";
import { cn, formatTimestamp } from "@/lib/utils";

import { ContentBlocks } from "./content-blocks";

/**
 * 终态 assistant 气泡。

 * 和 step-item **消费同一份 blocks**（§6.2）：思考在它产出的答案之上、
 * 思考之前发出的文本保持原位，这两条由 core 保证，组件只负责按序渲染。

 * 形制：**不加外框、不写"助手"**。界面里左右两侧已经把角色分开了（用户消息有气泡、
 * 助手消息是整段正文），再套一层矩形框只是把正文挤窄。时间戳保留成一行极小号灰字，
 * 有 `created_at` 才出现。

 * 这里也**不渲染运行时长**（§6.3）：那是按 run 解析的展示，只挂在列表组件
 * 选出的唯一锚点上，否则一次 run 里每条 AI 消息都会显示一份一模一样的。
 */
export interface AnswerBubbleProps {
  group: AnswerGroup;
  streaming?: boolean;
  className?: string;
}

export function AnswerBubble({ group, streaming = false, className }: AnswerBubbleProps) {
  const time = formatTimestamp(group.message.created_at);

  return (
    <article
      aria-label="助手回复"
      className={cn("w-full", className)}
      data-message-id={group.message.id}
    >
      {time ? (
        <p className="mb-1 text-[11px] leading-4 text-neutral-400">{time}</p>
      ) : null}
      <ContentBlocks blocks={group.blocks} streaming={streaming} />
    </article>
  );
}
