"use client";

import type { KnowledgeCitation } from "@/core/messages/citations";
import { withCitationLinks } from "@/core/messages/citations";
import type { ContentBlockView } from "@/core/messages/group";
import { cn } from "@/lib/utils";

import { Markdown } from "./markdown";
import { ThinkingBlock } from "./thinking-block";
import { ToolCallRow } from "./tool-call-row";

/**
 * 按序渲染一条消息的 blocks（M5 §6.1 / §6.2）。
 *
 * 这里是**两个渲染器共用的唯一一份实现**：流式期间的"处理中"分组（step-item）
 * 和结束后的终态气泡（answer-bubble）都走这个组件。如果两边各写一套
 * "思考放上面还是下面"的判断，用户就会在这一轮结束的瞬间看到它们交换位置。
 *
 * 组件里**只有 map**：不排序、不分组、不把思考提到最前——顺序在 core 的
 * buildContentBlocks 里就定死了。
 */
export interface ContentBlocksProps {
  blocks: readonly ContentBlockView[];
  streaming?: boolean;
  /** 终态回答才传：流式期间传 undefined，标记原样保留，避免链接闪烁。 */
  citations?: readonly KnowledgeCitation[];
  className?: string;
}

export function ContentBlocks({
  blocks,
  streaming = false,
  citations,
  className,
}: ContentBlocksProps) {
  return (
    <div className={cn("flex flex-col gap-3", className)}>
      {blocks.map((block, index) => {
        if (block.kind === "thinking") {
          // 思考块是"当前还在想"的标志：只有它排在最后一块时才在跑（答案一开始写，
          // 思考就结束了）——和文本块上那个光标用的是同一套判断。
          return (
            <ThinkingBlock
              key={block.id}
              running={streaming && index === blocks.length - 1}
              text={block.text}
            />
          );
        }
        if (block.kind === "text") {
          // 正文不挂光标（Markdown 自己也不支持）：流式追加时末尾不再有闪烁方块
          return (
            <Markdown
              key={block.id}
              text={
                citations === undefined
                  ? block.text
                  : withCitationLinks(block.text, citations)
              }
            />
          );
        }
        return (
          // 工具行和思考行同一形制（一行 + 点开看细节）：行距按行算，不再按卡片算
          <div className="flex flex-col gap-1.5" key={block.id}>
            {block.calls.map((call) => (
              <ToolCallRow call={call} key={call.id} />
            ))}
          </div>
        );
      })}
    </div>
  );
}
