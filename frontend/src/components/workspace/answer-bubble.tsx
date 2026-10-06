"use client";

import type { AnswerGroup } from "@/core/messages/group";
import { cn, formatTimestamp } from "@/lib/utils";

import { ContentBlocks } from "./content-blocks";

/**
 * 终态 assistant 气泡。
 *
 * 和 step-item **消费同一份 blocks**（§6.2）：思考在它产出的答案之上、
 * 思考之前发出的文本保持原位，这两条由 core 保证，组件只负责按序渲染。
 *
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
      className={cn("rounded-lg border border-neutral-200 bg-white px-4 py-3", className)}
      data-message-id={group.message.id}
    >
      <header className="mb-2 flex items-center gap-3 text-[11px] text-neutral-400">
        <span className="font-medium text-neutral-500">助手</span>
        {time ? <span>{time}</span> : null}
      </header>
      <ContentBlocks blocks={group.blocks} streaming={streaming} />
    </article>
  );
}
