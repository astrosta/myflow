"use client";

import { useState } from "react";

import { ChevronDownIcon, ToolIcon } from "@/components/ui/icons";
import { formatToolArgs, type ToolCallStatus, type ToolCallView } from "@/core/messages/tool-results";
import { cn } from "@/lib/utils";

/**
 * 工具调用行（和思考行同一形制，`thinking-block.tsx`）。
 *
 * 形制：一行 `🔧 工具 · <工具名> · <最关键的参数>`，点整行展开参数与结果。
 * 和 Think 一样**默认折叠**：一轮跑几十步时，卡片全摊开会把答案顶出屏幕；
 * 摘要里带着工具名和那个最像主语的参数，扫一眼就知道 Agent 干了什么。
 *
 * 四条约定：
 * 1. **参数和结果一个都不能少**（M5 §9 验收清单第一条）——它们只是收在展开区里，
 *    不是不显示；折叠时不挂载 DOM，长会话的节点数不会翻倍；
 * 2. 流式期间参数还是半截 JSON：摘要退回最后一个字符串值，正文用
 *    `formatToolArgs` 保住原文，不交给 `JSON.parse` 炸掉；
 * 3. 结果还没回来时状态是 pending，展开区显式写"等待结果…"，
 *    不要留一片空白让人以为是 bug；
 * 4. 状态只体现在行尾（失败标红）而不是一条彩色边框——一屏十几个工具调用，
 *    每条都描边会让步骤面板变成一堆框。
 */

export const TOOL_ROW_LABEL = "工具";

const STATUS_TEXT: Record<ToolCallStatus, string> = {
  pending: "运行中",
  success: "成功",
  error: "失败",
};

/**
 * 摘要取值的优先级。
 *
 * 排在前面的都是"这条调用到底在干什么"的主语（`command`/`query`/`path`），
 * 排在后面的才是补充说明。这张表比"取第一个字符串参数"稳：工具的 JSON 键序
 * 不保证，但写库的本意在这里。
 */
const SUMMARY_ARG_KEYS = [
  "command",
  "query",
  "keyword_query",
  "path",
  "file_path",
  "url",
  "pattern",
  "skill",
  "name",
  "title",
  "prompt",
  "text",
  "message",
  "thread_id",
  "knowledge_base_type",
];

/** 摘要长度上限：再长就截断，保证一行放得下 */
const SUMMARY_MAX_CHARS = 96;

function oneLine(text: string, limit = SUMMARY_MAX_CHARS): string {
  const squashed = text.replace(/\s+/g, " ").trim();
  return squashed.length <= limit ? squashed : squashed.slice(0, limit) + "…";
}

function primaryArg(call: ToolCallView): string {
  const args = call.args ?? {};
  for (const key of SUMMARY_ARG_KEYS) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return oneLine(value);
  }
  for (const value of Object.values(args)) {
    if (typeof value === "string" && value.trim()) return oneLine(value);
  }
  return "";
}

/** 半截 JSON 里最后一个字符串值：比原始花括号可读，也让摘要随着流式继续变长 */
function partialArg(argsText: string): string {
  const match = /"[^"]+"\s*:\s*"([^"]*)$/.exec(argsText);
  if (match?.[1]) return oneLine(match[1]);
  return oneLine(argsText.replace(/[{}[\]"]/g, " "));
}

/** 摘要里那个"主语参数"：行内单独一段渲染，才能和工具名用同样的间距分开 */
export function toolCallArg(call: ToolCallView): string {
  return primaryArg(call) || partialArg(call.argsText ?? "");
}

/** 整行摘要：`<工具名> · <参数摘要>`，没有可用参数时只有工具名 */
export function toolCallSummary(call: ToolCallView): string {
  const arg = toolCallArg(call);
  return arg ? call.name + " · " + arg : call.name;
}

export interface ToolCallRowProps {
  call: ToolCallView;
  defaultOpen?: boolean;
  className?: string;
}

export function ToolCallRow({ call, defaultOpen = false, className }: ToolCallRowProps) {
  const [open, setOpen] = useState(defaultOpen);

  const pending = call.status === "pending";
  const argsText = formatToolArgs(call);
  const status = STATUS_TEXT[call.status];
  const arg = toolCallArg(call);

  return (
    <section
      aria-label={"工具调用 " + call.name}
      className={cn("w-full", className)}
      data-state={call.status}
    >
      <button
        aria-expanded={open}
        aria-label={TOOL_ROW_LABEL + " " + call.name + "，" + status}
        className="group flex w-full items-center gap-1.5 rounded-md py-0.5 text-left text-xs transition-colors"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <ToolIcon className={cn("shrink-0 text-neutral-400", pending && "animate-pulse")} size={14} />
        <span className={cn("shrink-0 font-medium text-neutral-600", pending && "animate-pulse")}>
          {TOOL_ROW_LABEL}
        </span>
        <span aria-hidden className="shrink-0 text-neutral-300">
          ·
        </span>
        {/* 工具名和参数摘要各占一段：间隔交给 flex 的 gap，别用空格字符凑 */}
        <span className="flex min-w-0 flex-1 items-center gap-1.5 overflow-hidden text-xs text-neutral-400">
          <span className="shrink-0 text-neutral-500">{call.name}</span>
          {arg ? (
            <>
              <span aria-hidden className="shrink-0 text-neutral-300">
                ·
              </span>
              <span className="min-w-0 overflow-hidden text-ellipsis whitespace-nowrap">{arg}</span>
            </>
          ) : null}
        </span>
        {call.status === "error" ? (
          <span className="shrink-0 text-[11px] text-red-500">{status}</span>
        ) : null}
        <ChevronDownIcon
          className={cn(
            "shrink-0 text-neutral-300 transition-transform group-hover:text-neutral-500",
            open && "rotate-180",
          )}
          size={12}
        />
      </button>

      {pending ? (
        <span aria-live="polite" className="sr-only">
          工具运行中
        </span>
      ) : null}

      {open ? (
        <div className="mt-1 space-y-2 pl-6">
          <div>
            <p className="text-[11px] font-medium text-neutral-400">参数</p>
            {argsText ? (
              <pre className="mt-0.5 max-h-64 overflow-auto rounded bg-neutral-50 p-2 font-mono text-[11px] leading-relaxed text-neutral-700">
                {argsText}
              </pre>
            ) : (
              <p className="mt-0.5 text-xs text-neutral-400">（无参数）</p>
            )}
          </div>

          <div>
            <p className="text-[11px] font-medium text-neutral-400">结果</p>
            {call.status === "pending" ? (
              <p className="mt-0.5 text-xs text-neutral-400">等待结果…</p>
            ) : call.resultText ? (
              <pre
                className={cn(
                  "mt-0.5 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded p-2 font-mono text-[11px] leading-relaxed",
                  call.status === "error" ? "bg-red-50 text-red-800" : "bg-neutral-50 text-neutral-700",
                )}
              >
                {call.resultText}
              </pre>
            ) : (
              <p className="mt-0.5 text-xs text-neutral-400">（空结果）</p>
            )}
          </div>
        </div>
      ) : null}
    </section>
  );
}
