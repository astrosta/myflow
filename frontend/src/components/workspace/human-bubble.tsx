"use client";

import { textOf } from "@/core/messages/tool-results";
import type { ChatMessage } from "@/core/types";
import { cn, formatTimestamp } from "@/lib/utils";

/**
 * 用户消息气泡。
 *
 * 用户输入按纯文本渲染（**不走 markdown**）：自己刚打的字突然被解析成
 * 标题或列表，是纯粹的意外感。
 */
export interface HumanBubbleProps {
  message: ChatMessage;
  className?: string;
}

export function HumanBubble({ message, className }: HumanBubbleProps) {
  const text = typeof message.content === "string" ? message.content : textOf(message);
  const time = formatTimestamp(message.created_at);

  return (
    <article
      aria-label="用户消息"
      className={cn("flex flex-col items-end gap-1", className)}
      data-message-id={message.id}
    >
      <div className="max-w-[min(100%,42rem)] whitespace-pre-wrap break-words rounded-lg rounded-br-sm bg-neutral-100 px-4 py-2.5 text-sm leading-relaxed text-neutral-900">
        {text}
      </div>
      <div className="flex items-center gap-2 text-[11px] text-neutral-400">
        {message.optimistic ? <span>发送中…</span> : null}
        {time ? <span>{time}</span> : null}
      </div>
    </article>
  );
}
