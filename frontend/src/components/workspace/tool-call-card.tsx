"use client";

import { Badge } from "@/components/ui/badge";
import { formatToolArgs, type ToolCallStatus, type ToolCallView } from "@/core/messages/tool-results";
import { cn } from "@/lib/utils";

/**
 * 工具调用卡片。
 *
 * **参数和结果都必须显示**（M5 §9 验收清单第一条）：工具卡片是用户判断
 * "Agent 到底做了什么"的唯一入口，只显示工具名等于什么都没说。
 *
 * 流式期间参数还是半截 JSON，formatToolArgs 会退回原文；结果还没回来时
 * 状态是 pending，这里显式写"等待结果"，不要留一个空白区域让人以为是 bug。
 */

const STATUS_META: Record<ToolCallStatus, { label: string; variant: "neutral" | "success" | "danger" }> = {
  pending: { label: "运行中", variant: "neutral" },
  success: { label: "成功", variant: "success" },
  error: { label: "失败", variant: "danger" },
};

export interface ToolCallCardProps {
  call: ToolCallView;
  className?: string;
}

export function ToolCallCard({ call, className }: ToolCallCardProps) {
  const meta = STATUS_META[call.status];
  const argsText = formatToolArgs(call);

  return (
    <section
      aria-label={"工具调用 " + call.name}
      className={cn("rounded-lg border border-neutral-200 bg-white", className)}
    >
      <header className="flex items-center gap-2 border-b border-neutral-100 px-3 py-2">
        <span className="text-[11px] uppercase tracking-wide text-neutral-400">工具</span>
        <code className="min-w-0 flex-1 truncate font-mono text-xs font-medium text-neutral-800">{call.name}</code>
        <Badge variant={meta.variant}>{meta.label}</Badge>
      </header>

      <div className="space-y-2 px-3 py-2">
        <div>
          <p className="text-[11px] font-medium text-neutral-400">参数</p>
          {argsText ? (
            <pre className="mt-1 max-h-64 overflow-auto rounded bg-neutral-50 p-2 font-mono text-[11px] leading-relaxed text-neutral-700">
              {argsText}
            </pre>
          ) : (
            <p className="mt-1 text-xs text-neutral-400">（无参数）</p>
          )}
        </div>

        <div>
          <p className="text-[11px] font-medium text-neutral-400">结果</p>
          {call.status === "pending" ? (
            <p className="mt-1 text-xs text-neutral-400">等待结果…</p>
          ) : call.resultText ? (
            <pre
              className={cn(
                "mt-1 max-h-64 overflow-auto rounded p-2 font-mono text-[11px] leading-relaxed",
                call.status === "error" ? "bg-red-50 text-red-800" : "bg-neutral-50 text-neutral-700",
              )}
            >
              {call.resultText}
            </pre>
          ) : (
            <p className="mt-1 text-xs text-neutral-400">（空结果）</p>
          )}
        </div>
      </div>
    </section>
  );
}
