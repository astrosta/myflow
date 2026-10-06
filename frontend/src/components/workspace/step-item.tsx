"use client";

import type { StepView } from "@/core/messages/group";

import { ContentBlocks } from "./content-blocks";

/**
 * 一个步骤（一条 AI 消息）在"处理中"分组里的渲染。
 *
 * 一条 AI 消息可以同时有正文和 tool_calls（"这个方法失败了，我换一种"）——
 * blocks 里既有 text 也有 tool_calls，ContentBlocks 会把两者都渲染出来，
 * 不会因为"有工具调用"就把正文丢掉（§6.1）。
 */
export interface StepItemProps {
  step: StepView;
  streaming?: boolean;
}

export function StepItem({ step, streaming = false }: StepItemProps) {
  if (!step.blocks.length) return null;

  return (
    <li
      className="space-y-3 border-l border-neutral-200 pl-3"
      data-message-id={step.message.id}
      data-step-id={step.id}
    >
      <ContentBlocks blocks={step.blocks} streaming={streaming} />
    </li>
  );
}
