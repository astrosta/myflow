"use client";

import type { StreamRecoveryEvent } from "@/core/api/gap";
import { cn } from "@/lib/utils";

import { RecoveryBanner } from "./recovery-banner";

/**
 * 顶部状态条：streaming / error / recovery（M5 §5.2）。
 *
 * 三个状态**互斥**地占用这一条：recovery 非空时不再显示"就绪"或"已完成"，
 * 否则会在恢复过程中透出一句让人放心的话，正好是最危险的那种误导。
 */
export interface StreamStatusProps {
  streaming: boolean;
  threadLoading: boolean;
  error: string | null;
  recovery: StreamRecoveryEvent | null;
  onDismissRecovery: () => void;
  className?: string;
}

export function StreamStatus({
  streaming,
  threadLoading,
  error,
  recovery,
  onDismissRecovery,
  className,
}: StreamStatusProps) {
  if (recovery) {
    return <RecoveryBanner className={className} onDismiss={onDismissRecovery} recovery={recovery} />;
  }

  if (error) {
    return (
      <div
        aria-live="assertive"
        className={cn("flex items-center gap-2 border-b border-red-200 bg-red-50 px-4 py-2 text-xs text-red-800", className)}
        role="alert"
      >
        <span aria-hidden className="h-1.5 w-1.5 shrink-0 rounded-full bg-red-500" />
        <span className="min-w-0 flex-1 truncate" title={error}>
          出错了：{error}
        </span>
      </div>
    );
  }

  return (
    <div
      aria-live="polite"
      className={cn("flex items-center gap-2 border-b border-neutral-200 bg-white px-4 py-2 text-xs text-neutral-500", className)}
      role="status"
    >
      {streaming ? (
        <>
          <span aria-hidden className="stream-pulse h-1.5 w-1.5 rounded-full bg-emerald-500" />
          <span>正在生成…</span>
        </>
      ) : threadLoading ? (
        <>
          <span aria-hidden className="h-1.5 w-1.5 animate-pulse rounded-full bg-neutral-300" />
          <span>正在加载会话…</span>
        </>
      ) : (
        <>
          <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-neutral-300" />
          <span>就绪</span>
        </>
      )}
    </div>
  );
}
