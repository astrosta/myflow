"use client";

import { useId } from "react";

import type { DraftFile } from "@/core/api/skills";
import { cn } from "@/lib/utils";

export interface SkillFileTreeProps {
  files: readonly DraftFile[];
  selectedPath: string;
  onSelect: (path: string) => void;
  onUpload: (files: File[]) => void;
  uploading?: boolean;
  disabled?: boolean;
}

/** 把字节数写成人能一眼读懂的尺寸；文件树只做提示，不参与排序。 */
function formatSize(bytes: number): string {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

/**
 * 草稿文件树 + 上传附件。
 *
 * 每个路径是一个原生 button（不是 div + onClick）：键盘可达、有焦点样式，
 * 选中态用 aria-current 告诉读屏用户当前编辑的是哪一份文件。上传控件保留
 * 原生 file input（hidden 后用 label 触发），多选直接交给页面去调
 * uploadDraftFiles；这里不自己发请求，也不缓存上传结果。
 */
export function SkillFileTree({
  files,
  selectedPath,
  onSelect,
  onUpload,
  uploading = false,
  disabled = false,
}: SkillFileTreeProps) {
  const inputId = useId();

  return (
    <aside className="rounded-lg border border-neutral-200 bg-neutral-50/60 p-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-xs font-medium text-neutral-500">文件</h2>
        <label
          className={cn(
            "inline-flex h-7 cursor-pointer items-center rounded-md border border-neutral-300 bg-white px-2.5 text-[11px] font-medium text-neutral-700 transition-colors hover:border-neutral-400 hover:text-neutral-900",
            (uploading || disabled) && "cursor-not-allowed text-neutral-400",
          )}
          htmlFor={inputId}
        >
          {uploading ? "上传中…" : "上传附件"}
          <input
            aria-label="上传附件"
            className="sr-only"
            disabled={uploading || disabled}
            id={inputId}
            multiple
            onChange={(event) => {
              const selected = Array.from(event.target.files ?? []);
              // 清空 value，连续选择同一个文件也能再次触发 change。
              event.target.value = "";
              if (selected.length > 0) onUpload(selected);
            }}
            type="file"
          />
        </label>
      </div>

      {files.length === 0 ? (
        <p className="mt-3 text-xs leading-relaxed text-neutral-400">
          还没有文件。上传附件，或直接在右侧编写 SKILL.md。
        </p>
      ) : (
        <ul className="mt-2 space-y-0.5">
          {files.map((file) => {
            const selected = file.path === selectedPath;
            return (
              <li key={file.path}>
                <button
                  aria-current={selected ? "true" : undefined}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors",
                    selected
                      ? "bg-neutral-900 text-white"
                      : "text-neutral-700 hover:bg-neutral-200/70 hover:text-neutral-900",
                  )}
                  onClick={() => onSelect(file.path)}
                  title={file.path + " · " + formatSize(file.size_bytes)}
                  type="button"
                >
                  <span className="min-w-0 flex-1 truncate font-mono">{file.path}</span>
                  <span
                    aria-hidden="true"
                    className={cn("shrink-0 text-[10px]", selected ? "text-neutral-300" : "text-neutral-400")}
                  >
                    {formatSize(file.size_bytes)}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </aside>
  );
}
