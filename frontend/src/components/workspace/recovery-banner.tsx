"use client";

import type { StreamRecoveryEvent } from "@/core/api/gap";
import { cn } from "@/lib/utils";

/**
 * SSE gap 恢复提示（M5 §5.2）。
 *
 * **文案的红线：绝不能显示成"已完成"。** 收到 gap 只说明"你要的那段缓冲区
 * 已经滚过去了"，后端的 run 还在跑。界面要是显示完成，用户会以为回答就这么短，
 * 然后去做别的事。
 *
 * 恢复次数用尽时也要说清楚"可能仍在后台运行"，而不是把红色错误当成一次收尾。
 */
export interface RecoveryBannerProps {
  recovery: StreamRecoveryEvent;
  onDismiss?: () => void;
  className?: string;
}

export function RecoveryBanner({ recovery, onDismiss, className }: RecoveryBannerProps) {
  const detail =
    "已请求事件 " +
    (recovery.frame.requested_event_id ?? "（无）") +
    " · 缓冲区最早 " +
    (recovery.frame.earliest_available_event_id ?? "（空）") +
    " · 最新 " +
    (recovery.frame.latest_available_event_id ?? "（无）");

  return (
    <div
      aria-live="polite"
      className={cn(
        "flex items-start gap-3 border-b px-4 py-2.5 text-xs",
        recovery.exhausted
          ? "border-red-200 bg-red-50 text-red-800"
          : "border-amber-200 bg-amber-50 text-amber-900",
        className,
      )}
      role="status"
    >
      <span aria-hidden className={cn("mt-0.5 h-1.5 w-1.5 shrink-0 rounded-full", recovery.exhausted ? "bg-red-500" : "stream-pulse bg-amber-500")} />
      <div className="min-w-0 flex-1 space-y-1">
        <p className="font-medium">
          {recovery.exhausted ? "连接中断，自动恢复失败" : "连接中断，正在恢复…"}
        </p>
        <p className="leading-relaxed">
          {recovery.exhausted
            ? "重试次数已用尽。后台的 run 可能仍在运行，这一轮并没有结束 —— 请刷新页面重新加入。"
            : "实时流的一段已经不可回放，正在重载会话状态并重新订阅（第 " +
              recovery.attempt +
              "/" +
              recovery.maxAttempts +
              " 次）。恢复完成前不要认为对话已结束。"}
        </p>
        <p className="truncate text-[11px] opacity-70" title={detail}>
          {detail}
        </p>
      </div>
      {onDismiss ? (
        <button
          className="shrink-0 rounded px-2 py-1 text-[11px] font-medium underline-offset-2 hover:underline"
          onClick={onDismiss}
          type="button"
        >
          知道了
        </button>
      ) : null}
    </div>
  );
}
