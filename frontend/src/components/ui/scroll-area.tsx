import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

export function ScrollArea({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("thin-scrollbar overflow-y-auto overscroll-contain", className)} {...props} />;
}
