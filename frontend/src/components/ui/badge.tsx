import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

export interface BadgeProps extends ComponentProps<"span"> {
  variant?: "neutral" | "success" | "danger" | "warning" | "outline";
}

const VARIANTS = {
  neutral: "bg-neutral-100 text-neutral-600",
  success: "bg-emerald-50 text-emerald-700",
  danger: "bg-red-50 text-red-700",
  warning: "bg-amber-50 text-amber-700",
  outline: "border border-neutral-300 text-neutral-600",
};

export function Badge({ className, variant = "neutral", ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium leading-none",
        VARIANTS[variant],
        className,
      )}
      {...props}
    />
  );
}
