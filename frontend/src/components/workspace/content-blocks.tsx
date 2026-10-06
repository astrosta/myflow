"use client";

import type { ContentBlockView } from "@/core/messages/group";
import { cn } from "@/lib/utils";

import { Markdown } from "./markdown";
import { ThinkingBlock } from "./thinking-block";
import { ToolCallCard } from "./tool-call-card";

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
  className?: string;
}

export function ContentBlocks({ blocks, streaming = false, className }: ContentBlocksProps) {
  return (
    <div className={cn("flex flex-col gap-3", className)}>
      {blocks.map((block, index) => {
        if (block.kind === "thinking") {
          return <ThinkingBlock key={block.id} text={block.text} />;
        }
        if (block.kind === "text") {
          // 光标只挂在最后一块上：中间那些文本块已经"说完"了
          return (
            <Markdown
              key={block.id}
              streaming={streaming && index === blocks.length - 1}
              text={block.text}
            />
          );
        }
        return (
          <div className="flex flex-col gap-2" key={block.id}>
            {block.calls.map((call) => (
              <ToolCallCard call={call} key={call.id} />
            ))}
          </div>
        );
      })}
    </div>
  );
}
