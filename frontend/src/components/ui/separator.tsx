import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

export interface SeparatorProps extends ComponentProps<"div"> {
  orientation?: "horizontal" | "vertical";
}

export function Separator({ className, orientation = "horizontal", ...props }: SeparatorProps) {
  return (
    <div
      aria-orientation={orientation}
      className={cn("shrink-0 bg-neutral-200", orientation === "horizontal" ? "h-px w-full" : "h-full w-px", className)}
      role="separator"
      {...props}
    />
  );
}
