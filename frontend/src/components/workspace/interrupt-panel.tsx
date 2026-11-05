"use client";

import { Button } from "@/components/ui/button";
import type { ChatInterrupt } from "@/core/threads/interrupts";

export interface InterruptPanelProps {
  interrupt: ChatInterrupt;
  busy?: boolean;
  onDecide: (decision: "approve" | "reject") => void;
}

/**
 * Graph-level HITL confirmation.
 *
 * LangGraph has already paused before the tool node; approving resumes the
 * same thread with Command(resume=...), and rejecting resumes it with a
 * synthetic error ToolMessage so the model can continue without writing.
 */
export function InterruptPanel({ interrupt, busy = false, onDecide }: InterruptPanelProps) {
  const canApprove = interrupt.allowedDecisions.includes("approve");
  const canReject = interrupt.allowedDecisions.includes("reject");
  const titleId = "interrupt-panel-title";

  return (
    <section
      aria-labelledby={titleId}
      className="rounded-lg border border-amber-200 bg-amber-50/80 px-4 py-3 text-sm text-amber-950 shadow-sm"
      role="alertdialog"
    >
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <h2 className="font-semibold" id={titleId}>
          需要你确认
        </h2>
        <span className="text-xs text-amber-700">工具 · {interrupt.toolName}</span>
      </div>
      <p className="mt-2 whitespace-pre-wrap leading-relaxed text-amber-900">
        {interrupt.description}
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {canReject ? (
          <Button disabled={busy} onClick={() => onDecide("reject")} size="sm" variant="secondary">
            拒绝
          </Button>
        ) : null}
        {canApprove ? (
          <Button disabled={busy} onClick={() => onDecide("approve")} size="sm" variant="primary">
            {busy ? "处理中…" : "允许并继续"}
          </Button>
        ) : null}
        {interrupt.actionCount > 1 ? (
          <span className="text-xs text-amber-700">共 {interrupt.actionCount} 项操作</span>
        ) : null}
      </div>
    </section>
  );
}
