import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

export interface ButtonProps extends ComponentProps<"button"> {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md" | "icon";
}

const VARIANTS = {
  primary: "bg-neutral-900 text-white hover:bg-neutral-700 disabled:bg-neutral-300",
  secondary:
    "border border-neutral-300 bg-white text-neutral-700 hover:border-neutral-400 hover:text-neutral-900 disabled:text-neutral-400",
  ghost: "text-neutral-600 hover:bg-neutral-100 hover:text-neutral-900 disabled:text-neutral-300",
  danger: "border border-red-200 bg-white text-red-700 hover:bg-red-50 disabled:text-red-300",
};

const SIZES = {
  sm: "h-8 px-3 text-xs",
  md: "h-9 px-4 text-sm",
  icon: "h-9 w-9 text-sm",
};

export function Button({ className, variant = "secondary", size = "md", type = "button", ...props }: ButtonProps) {
  return (
    <button
      className={cn(
        "inline-flex shrink-0 items-center justify-center gap-2 rounded-md font-medium transition-colors disabled:cursor-not-allowed",
        VARIANTS[variant],
        SIZES[size],
        className,
      )}
      type={type}
      {...props}
    />
  );
}
