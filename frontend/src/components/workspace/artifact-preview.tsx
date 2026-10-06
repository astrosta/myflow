"use client";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import type { ArtifactPreview as ArtifactPreviewData } from "@/core/api/artifacts";
import { ARTIFACT_PREVIEW_MAX_BYTES } from "@/core/types";
import { cn, formatBytes } from "@/lib/utils";

/**
 * 只读 artifact 预览（M5 §6.5）。
 *
 * 两条刻意的取舍：
 *
 * 1. **不挂重型代码编辑器**（Monaco / CodeMirror / shiki 运行时）。预览的
 *    目的是"看一眼内容对不对"，为了高亮去拉一个几百 KB 的编辑器不划算。
 *    这里就是一个 pre，配色交给 chat.css。
 * 2. **被截断时给一个显式按钮**加载完整文件，而不是默认就把整个文件拉下来。
 */

export type ArtifactPreviewState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ready"; preview: ArtifactPreviewData }
  | { status: "error"; message: string };

export interface ArtifactPreviewProps {
  state: ArtifactPreviewState;
  /** 已经点过"加载完整文件" */
  fullLoaded?: boolean;
  onLoadFull?: () => void;
  className?: string;
}

export function ArtifactPreview({ state, fullLoaded = false, onLoadFull, className }: ArtifactPreviewProps) {
  if (state.status === "idle") {
    return <p className={cn("px-3 py-6 text-center text-xs text-neutral-400", className)}>选择一个文件查看内容。</p>;
  }

  if (state.status === "loading") {
    return (
      <div className={cn("space-y-2 p-3", className)} aria-busy>
        <Skeleton className="h-4 w-1/3" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <p className={cn("p-3 text-xs leading-relaxed text-red-700", className)} role="alert">
        预览加载失败：{state.message}
      </p>
    );
  }

  const { preview } = state;

  return (
    <div className={cn("flex min-h-0 flex-col gap-2 p-3", className)}>
      {preview.truncated ? (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-900">
          <span>
            已显示前 {formatBytes(ARTIFACT_PREVIEW_MAX_BYTES)}
            {preview.totalBytes !== null ? "（共 " + formatBytes(preview.totalBytes) + "）" : ""}
          </span>
          {onLoadFull ? (
            <Button disabled={fullLoaded} onClick={onLoadFull} size="sm" variant="secondary">
              {fullLoaded ? "已加载完整文件" : "加载完整文件"}
            </Button>
          ) : null}
        </div>
      ) : null}

      <pre className="thin-scrollbar max-h-[60vh] min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words rounded border border-neutral-200 bg-neutral-50 p-2 font-mono text-[11px] leading-relaxed text-neutral-700">
        {preview.text}
      </pre>
    </div>
  );
}
