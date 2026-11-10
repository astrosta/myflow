"use client";

import { cn } from "@/lib/utils";

/**
 * 统一的启用开关。
 *
 * 技能中心和工具管理共用同一套尺寸与动效：30×16 细轨、12px 滑块、
 * emerald-500 / neutral-300 两态、180ms 过渡，不再让两个页面各画一个。
 */
export interface EnabledSwitchProps {
  checked: boolean;
  onToggle: () => void;
  /** 必须带上目标名称；读屏器不能让一屏开关都叫同一个名字。 */
  label: string;
  disabled?: boolean;
  title?: string;
}

export function EnabledSwitch({
  checked,
  onToggle,
  label,
  disabled = false,
  title,
}: EnabledSwitchProps) {
  return (
    <button
      aria-checked={checked}
      aria-label={label}
      className="inline-flex shrink-0 cursor-pointer items-center rounded-full border-0 bg-transparent p-0.5 outline-none disabled:cursor-not-allowed disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-emerald-500/40"
      disabled={disabled}
      onClick={onToggle}
      role="switch"
      title={title}
      type="button"
    >
      <span
        aria-hidden="true"
        className={cn(
          "relative block h-4 w-[30px] shrink-0 rounded-full transition-colors duration-[180ms]",
          checked ? "bg-emerald-500" : "bg-neutral-300",
        )}
      >
        <span
          className={cn(
            "absolute top-0.5 h-3 w-3 rounded-full bg-white shadow-[0_1px_2px_rgba(0,0,0,0.25)] transition-[left] duration-[180ms]",
            checked ? "left-4" : "left-0.5",
          )}
        />
      </span>
    </button>
  );
}
