"use client";

import { useEffect, useRef, type ComponentProps } from "react";

import { cn } from "@/lib/utils";

export interface TextareaProps extends ComponentProps<"textarea"> {
  /** 随内容长高（上限 maxHeight）。聊天输入框需要，普通表单不需要 */
  autoGrow?: boolean;
  maxHeight?: number;
}

export function Textarea({ className, autoGrow = false, maxHeight = 240, value, ...props }: TextareaProps) {
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!autoGrow) return;
    const element = ref.current;
    if (!element) return;
    // 先归零再读 scrollHeight，否则删字之后高度不会缩回去
    element.style.height = "auto";
    element.style.height = Math.min(element.scrollHeight, maxHeight) + "px";
  }, [autoGrow, maxHeight, value]);

  return (
    <textarea
      className={cn(
        "w-full resize-none rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm leading-relaxed",
        "text-neutral-900 placeholder:text-neutral-400",
        "focus:border-neutral-400 focus:outline-none disabled:bg-neutral-50 disabled:text-neutral-400",
        className,
      )}
      ref={ref}
      value={value}
      {...props}
    />
  );
}
