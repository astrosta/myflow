"use client";

import { useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";

export interface TabItem {
  id: string;
  label: ReactNode;
  content: ReactNode;
  disabled?: boolean;
}

export interface TabsProps {
  items: readonly TabItem[];
  value?: string;
  defaultValue?: string;
  onValueChange?: (id: string) => void;
  className?: string;
  listClassName?: string;
}

/** 极简标签页：只做切换和无障碍角色，不做动画和键盘漫游 */
export function Tabs({ items, value, defaultValue, onValueChange, className, listClassName }: TabsProps) {
  const [uncontrolled, setUncontrolled] = useState(defaultValue ?? items[0]?.id ?? "");
  const active = value ?? uncontrolled;
  const activeItem = items.find((item) => item.id === active) ?? items[0];

  const select = (id: string) => {
    if (value === undefined) setUncontrolled(id);
    onValueChange?.(id);
  };

  return (
    <div className={cn("flex min-h-0 flex-col", className)}>
      <div className={cn("flex items-center gap-1 border-b border-neutral-200", listClassName)} role="tablist">
        {items.map((item) => {
          const selected = item.id === activeItem?.id;
          return (
            <button
              aria-selected={selected}
              className={cn(
                "-mb-px border-b-2 px-3 py-2 text-xs font-medium transition-colors",
                selected ? "border-neutral-900 text-neutral-900" : "border-transparent text-neutral-500 hover:text-neutral-800",
                item.disabled ? "cursor-not-allowed opacity-50" : "",
              )}
              disabled={item.disabled}
              key={item.id}
              onClick={() => select(item.id)}
              role="tab"
              type="button"
            >
              {item.label}
            </button>
          );
        })}
      </div>
      <div className="min-h-0 flex-1" role="tabpanel">
        {activeItem?.content}
      </div>
    </div>
  );
}
