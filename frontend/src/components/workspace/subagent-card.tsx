"use client";

import { UserIcon } from "@/components/ui/icons";
import { Collapsible } from "@/components/ui/collapsible";
import type { SubagentStatus, SubagentView } from "@/core/messages/subagent";
import { cn } from "@/lib/utils";

/**
 * 子任务卡片（task 工具）。
 *
 * task 在协议上是普通工具调用，但用户看到的是「派了一个帮手」：description 是
 * 任务标签，状态来自 task_* 事件/台账，折叠区里是中间步骤、结论和任务简报。
 * 不要再退回 ToolCallRow——那会把 prompt 当成工具参数摘要铺在标题里。
 */

const STATUS_TONE: Record<SubagentStatus, string> = {
  pending: "text-neutral-500",
  running: "text-emerald-600",
  completed: "text-emerald-700",
  failed: "text-red-600",
  cancelled: "text-neutral-500",
  timed_out: "text-amber-700",
  polling_timed_out: "text-amber-700",
};

const STOP_REASON_LABEL: Record<string, string> = {
  token_capped: "达到 token 预算，结论为部分结果",
  turn_capped: "达到轮次预算，结论为部分结果",
  loop_capped: "检测到循环，提前停止",
};

function formatUsage(usage: SubagentView["usage"]): string {
  if (!usage) return "";
  if (usage.total_tokens !== undefined) return usage.total_tokens + " tokens";
  const parts: string[] = [];
  if (usage.input_tokens !== undefined) parts.push("输入 " + usage.input_tokens);
  if (usage.output_tokens !== undefined) parts.push("输出 " + usage.output_tokens);
  return parts.join(" · ");
}

export interface SubagentCardProps {
  view: SubagentView;
  className?: string;
}

export function SubagentCard({ view, className }: SubagentCardProps) {
  const running = view.status === "running" || view.status === "pending";
  const usage = formatUsage(view.usage);
  const stopReason = view.stopReason
    ? (STOP_REASON_LABEL[view.stopReason] ?? view.stopReason)
    : null;
  const partial = view.status === "completed" && Boolean(stopReason);
  const startedAt =
    view.startedAt && !Number.isNaN(Date.parse(view.startedAt))
      ? new Date(view.startedAt).toLocaleString()
      : null;

  return (
    <section
      aria-label={"子任务 " + view.description}
      className={cn(
        "rounded-lg border border-neutral-200 bg-neutral-50/60 px-3 py-2.5",
        view.status === "failed" ? "border-red-200 bg-red-50/40" : "",
        partial ? "border-amber-200 bg-amber-50/30" : "",
        className,
      )}
      data-status={view.status}
    >
      <Collapsible
        contentClassName="space-y-3 border-t border-neutral-100 pt-2.5"
        trigger={
          <span className="flex min-w-0 items-center gap-2">
            <UserIcon
              className={cn("shrink-0 text-neutral-400", running && "animate-pulse")}
              size={14}
            />
            <span className="shrink-0 font-medium text-neutral-600">子任务</span>
            <span aria-hidden className="shrink-0 text-neutral-300">
              ·
            </span>
            <span className="truncate text-neutral-700">{view.description}</span>
            {view.subagentType ? (
              <span className="shrink-0 rounded bg-neutral-100 px-1.5 py-0.5 font-mono text-[10px] text-neutral-500">
                {view.subagentType}
              </span>
            ) : null}
          </span>
        }
        meta={
          <span className={cn("font-medium", STATUS_TONE[view.status])}>
            {view.statusLabel}
            {partial ? " · 部分结果" : ""}
            {view.steps.length > 0 ? " · " + view.steps.length + " 步" : ""}
          </span>
        }
      >
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-neutral-500">
          {view.modelName ? <span>模型 {view.modelName}</span> : null}
          {usage ? <span>{usage}</span> : null}
          {startedAt ? <span>开始 {startedAt}</span> : null}
        </div>

        {stopReason ? (
          <p className="rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800">
            {stopReason}
          </p>
        ) : null}

        {view.steps.length > 0 ? (
          <div>
            <p className="text-[11px] font-medium text-neutral-400">执行步骤</p>
            <ol aria-label="子代理步骤" className="mt-1 space-y-2">
              {view.steps.map((step) => (
                <li key={step.key} className="rounded border border-neutral-200 bg-white px-2 py-1.5">
                  <p className="font-mono text-[10px] text-neutral-400">
                    {step.kind === "tool"
                      ? "工具" + (step.name ? " · " + step.name : "")
                      : step.kind === "ai"
                        ? "子代理输出"
                        : "步骤"}
                    {step.toolNames.length > 0 ? " → " + step.toolNames.join(", ") : ""}
                  </p>
                  {step.text ? (
                    <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-neutral-600">
                      {step.text}
                    </pre>
                  ) : null}
                </li>
              ))}
            </ol>
          </div>
        ) : running ? (
          <p className="text-[11px] text-neutral-400">子任务已开始，等待第一步…</p>
        ) : null}

        {view.error ? (
          <div>
            <p className="text-[11px] font-medium text-red-500">失败原因</p>
            <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-red-50 p-2 font-mono text-[11px] leading-relaxed text-red-800">
              {view.error}
            </pre>
          </div>
        ) : null}

        {view.resultText ? (
          <div>
            <p className="text-[11px] font-medium text-neutral-400">
              {view.status === "completed" ? "最终结论" : "部分结果"}
            </p>
            <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words rounded bg-white p-2 font-mono text-[11px] leading-relaxed text-neutral-700">
              {view.resultText}
            </pre>
          </div>
        ) : null}

        {view.prompt ? (
          <div>
            <p className="text-[11px] font-medium text-neutral-400">任务简报</p>
            <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded bg-white p-2 font-mono text-[11px] leading-relaxed text-neutral-500">
              {view.prompt}
            </pre>
          </div>
        ) : null}
      </Collapsible>
    </section>
  );
}
