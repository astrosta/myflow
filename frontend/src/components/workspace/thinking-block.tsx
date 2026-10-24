"use client";

import { useEffect, useRef, useState } from "react";

import { ChevronDownIcon, ThinkIcon } from "@/components/ui/icons";
import { cn } from "@/lib/utils";

/**
 * 思考块（参考 DeepSeek Harness 的 ReasoningRow）。
 *
 * 形制：一行 `💡 Think · <最新一句>`，**思考流式输出时那一段跟着动**（取最后一行，
 * 超出宽度显示尾部，因为用户关心的是"它现在想到哪了"；结束后改显示第一行），
 * 点这一行展开完整思考文本。
 *
 * 三条约束：
 * 1. 默认折叠：思考文本可以很长，展开会把答案挤出屏幕（M5 §6.2）；
 * 2. 它**只渲染传进来的文本**，在答案上面还是下面由 core 的 blocks 决定；
 * 3. 折叠时不挂载正文 DOM（长会话节点数别翻几倍）。
 */
export interface ThinkingBlockProps {
  text: string;
  /** 这一轮还在流式输出：摘要取最后一行并跟随，加呼吸提示 */
  running?: boolean;
  defaultOpen?: boolean;
}

export const THINK_LABEL = "Think";

/** 摘要宽度上限：再长就截尾部，保证一行放得下 */
const SUMMARY_MAX_CHARS = 120;

function firstLine(text: string): string {
  return text.trim().split("\n").find((line) => line.trim().length > 0) ?? "";
}

function latestLine(text: string): string {
  const lines = text.trimEnd().split("\n").filter((line) => line.trim().length > 0);
  return lines.length ? lines[lines.length - 1]!.trim() : "";
}

function tailOf(line: string, limit = SUMMARY_MAX_CHARS): string {
  const text = line.trim();
  return text.length <= limit ? text : "…" + text.slice(text.length - limit);
}

function headOf(line: string, limit = SUMMARY_MAX_CHARS): string {
  const text = line.trim();
  return text.length <= limit ? text : text.slice(0, limit) + "…";
}

export function ThinkingBlock({ text, running = false, defaultOpen = false }: ThinkingBlockProps) {
  const [open, setOpen] = useState(defaultOpen);
  const summaryRef = useRef<HTMLSpanElement | null>(null);

  // 流式时把摘要滚到尾部：一行放不下时，用户要看的是刚想到的那几个字
  useEffect(() => {
    const element = summaryRef.current;
    if (!element || !running) return;
    element.scrollLeft = element.scrollWidth;
  }, [running, text]);

  const summary = running ? tailOf(latestLine(text)) : headOf(firstLine(text));

  return (
    <div className="w-full" data-state={running ? "running" : "ok"}>
      <button
        aria-expanded={open}
        aria-label={running ? THINK_LABEL + "，正在思考" : THINK_LABEL}
        className="group flex w-full items-center gap-1.5 rounded-md py-0.5 text-left text-xs transition-colors"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <ThinkIcon
          className={cn("shrink-0 text-neutral-400", running && "animate-pulse")}
          size={14}
        />
        <span className={cn("shrink-0 font-medium text-neutral-600", running && "animate-pulse")}>
          {THINK_LABEL}
        </span>
        <span aria-hidden className="shrink-0 text-neutral-300">
          ·
        </span>
        <span
          className="min-w-0 flex-1 overflow-hidden whitespace-nowrap text-neutral-400"
          ref={summaryRef}
        >
          {summary}
        </span>
        <ChevronDownIcon
          className={cn(
            "shrink-0 text-neutral-300 transition-transform group-hover:text-neutral-500",
            open && "rotate-180",
          )}
          size={12}
        />
      </button>

      {running ? (
        <span aria-live="polite" className="sr-only">
          正在思考
        </span>
      ) : null}

      {open ? (
        <pre className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap break-words pl-6 font-sans text-xs leading-relaxed text-neutral-500">
          {text}
        </pre>
      ) : null}
    </div>
  );
}
