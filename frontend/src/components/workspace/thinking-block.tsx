"use client";

import { Collapsible } from "@/components/ui/collapsible";

/**
 * 思考块。默认折叠（M5 §6.2）：思考过程很长，展开状态下会把答案挤到屏幕外。
 *
 * 它**只负责渲染传进来的那段文本**，不参与任何排序——
 * 它在答案上面还是下面，由 core 的 blocks 数组决定，组件没有决定权。
 */
export interface ThinkingBlockProps {
  text: string;
  defaultOpen?: boolean;
}

function firstLine(text: string): string {
  const line = text.trim().split("\n").find((item) => item.trim().length > 0) ?? "";
  return line.length > 80 ? line.slice(0, 80) + "…" : line;
}

export function ThinkingBlock({ text, defaultOpen = false }: ThinkingBlockProps) {
  return (
    <Collapsible
      contentClassName="rounded-md border border-neutral-200 bg-neutral-50/70 p-3"
      defaultOpen={defaultOpen}
      meta={text.length + " 字"}
      trigger={
        <span className="inline-flex min-w-0 items-baseline gap-2">
          <span className="shrink-0 text-neutral-500">思考过程</span>
          <span className="truncate font-normal text-neutral-400">{firstLine(text)}</span>
        </span>
      }
    >
      <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words font-sans text-xs leading-relaxed text-neutral-600">
        {text}
      </pre>
    </Collapsible>
  );
}
