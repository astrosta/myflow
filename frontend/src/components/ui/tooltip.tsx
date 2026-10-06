"use client";

import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * 悬停提示。用纯 CSS 实现（group-hover），不引定位库：
 * 这里的用法都是"图标按钮旁边补一句说明"，不需要碰撞检测和 portal。
 */
export interface TooltipProps {
  label: string;
  children: ReactNode;
  side?: "top" | "bottom";
  className?: string;
}

export function Tooltip({ label, children, side = "top", className }: TooltipProps) {
  return (
    <span className={cn("group relative inline-flex", className)}>
      {children}
      <span
        className={cn(
          "pointer-events-none absolute left-1/2 z-20 hidden -translate-x-1/2 whitespace-nowrap rounded bg-neutral-900 px-2 py-1 text-[11px] text-white",
          "group-hover:block group-focus-within:block",
          side === "top" ? "bottom-full mb-1.5" : "top-full mt-1.5",
        )}
        role="tooltip"
      >
        {label}
      </span>
    </span>
  );
}
