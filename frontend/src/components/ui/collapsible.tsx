"use client";

import { useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * 折叠面板。受控/非受控两用——思考块、步骤分组、artifact 面板都用它。
 *
 * 折叠时内容**卸载**而不是用 CSS 隐藏：思考文本可以很长，
 * 留着 DOM 会让一个长会话的节点数白白翻几倍。
 */
export interface CollapsibleProps {
  /** 触发器内容 */
  trigger: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  className?: string;
  contentClassName?: string;
  /** 触发器右侧的附加说明（计数、耗时之类） */
  meta?: ReactNode;
}

export function Collapsible({
  trigger,
  children,
  defaultOpen = false,
  open,
  onOpenChange,
  className,
  contentClassName,
  meta,
}: CollapsibleProps) {
  const [uncontrolled, setUncontrolled] = useState(defaultOpen);
  const isOpen = open ?? uncontrolled;

  const toggle = () => {
    const next = !isOpen;
    if (open === undefined) setUncontrolled(next);
    onOpenChange?.(next);
  };

  return (
    <div className={cn("w-full", className)}>
      <button
        aria-expanded={isOpen}
        className="flex w-full items-center gap-2 rounded-md text-left text-xs font-medium text-neutral-500 transition-colors hover:text-neutral-800"
        onClick={toggle}
        type="button"
      >
        <span
          aria-hidden
          className={cn("inline-block text-[10px] text-neutral-400 transition-transform", isOpen ? "rotate-90" : "rotate-0")}
        >
          ▶
        </span>
        <span className="min-w-0 flex-1 truncate">{trigger}</span>
        {meta ? <span className="shrink-0 text-[11px] text-neutral-400">{meta}</span> : null}
      </button>
      {isOpen ? <div className={cn("mt-2", contentClassName)}>{children}</div> : null}
    </div>
  );
}
