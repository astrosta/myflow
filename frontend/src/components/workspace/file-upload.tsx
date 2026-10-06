"use client";

import { useRef, useState, type ChangeEvent } from "react";

import { Button } from "@/components/ui/button";
import { cn, formatBytes } from "@/lib/utils";

/**
 * 文件选择。只负责"选了哪些文件"，上传时机由 composer 决定
 * （提交时和文本一起交给 controller.submit）。
 *
 * 上限用 NEXT_PUBLIC_MAX_UPLOAD_BYTES，和 nginx 的 upload 路由对齐；
 * 在这里先拦一次是为了给用户一个立刻的反馈，而不是传一半才失败。
 */
export const MAX_UPLOAD_BYTES = Number(process.env.NEXT_PUBLIC_MAX_UPLOAD_BYTES ?? 26214400);

export interface FileUploadProps {
  files: readonly File[];
  onChange: (files: File[]) => void;
  disabled?: boolean;
  maxBytes?: number;
  className?: string;
}

export function FileUpload({
  files,
  onChange,
  disabled = false,
  maxBytes = MAX_UPLOAD_BYTES,
  className,
}: FileUploadProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);

  const handlePick = (event: ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(event.target.files ?? []);
    const accepted = picked.filter((file) => file.size <= maxBytes);
    const rejected = picked.filter((file) => file.size > maxBytes);
    setError(
      rejected.length ? rejected.map((file) => file.name).join("、") + " 超过大小上限，已跳过" : null,
    );
    if (accepted.length) onChange([...files, ...accepted]);
    // 清空 input：否则连续两次选同一个文件不会触发 change
    event.target.value = "";
  };

  const removeAt = (index: number) => {
    onChange(files.filter((_, current) => current !== index));
  };

  return (
    <div className={cn("flex min-w-0 flex-wrap items-center gap-2", className)}>
      <input
        className="hidden"
        disabled={disabled}
        multiple
        onChange={handlePick}
        ref={inputRef}
        type="file"
      />
      <Button disabled={disabled} onClick={() => inputRef.current?.click()} size="sm" variant="ghost">
        <span aria-hidden>＋</span>
        添加文件
      </Button>

      {files.map((file, index) => (
        <span
          className="inline-flex max-w-[14rem] items-center gap-1 rounded border border-neutral-200 bg-neutral-50 px-2 py-1 text-[11px] text-neutral-600"
          key={file.name + "-" + String(index)}
        >
          <span className="truncate" title={file.name}>
            {file.name}
          </span>
          <span className="shrink-0 text-neutral-400">{formatBytes(file.size)}</span>
          <button
            aria-label={"移除 " + file.name}
            className="shrink-0 rounded px-1 text-neutral-400 hover:text-neutral-700"
            disabled={disabled}
            onClick={() => removeAt(index)}
            type="button"
          >
            ×
          </button>
        </span>
      ))}

      {error ? (
        <span className="text-[11px] text-red-600" role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}
