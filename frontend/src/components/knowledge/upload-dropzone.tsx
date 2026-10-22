"use client";

import { useRef, useState, type ChangeEvent, type DragEvent } from "react";

import { Button } from "@/components/ui/button";
import { cn, formatBytes } from "@/lib/utils";

export interface UploadDropzoneProps {
  allowedExtensions: readonly string[];
  maxFileSizeBytes: number;
  maxFiles: number;
  disabled?: boolean;
  uploading?: boolean;
  onUpload: (files: File[]) => void;
}

const CARD = "rounded-lg border border-dashed border-neutral-300 bg-neutral-50/60 px-4 py-5";

export function UploadDropzone({
  allowedExtensions,
  maxFileSizeBytes,
  maxFiles,
  disabled = false,
  uploading = false,
  onUpload,
}: UploadDropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const accept = allowedExtensions.map((ext) => "." + ext).join(",");

  const acceptFiles = (picked: File[]) => {
    if (disabled || uploading || !picked.length) return;
    const rejectedType = picked.filter((file) => {
      const ext = file.name.includes(".") ? file.name.split(".").pop()?.toLowerCase() : "";
      return !ext || !allowedExtensions.includes(ext);
    });
    const rejectedSize = picked.filter((file) => file.size > maxFileSizeBytes);
    if (rejectedType.length) {
      setError("不支持的文件类型：" + rejectedType.map((file) => file.name).join("、"));
      return;
    }
    if (rejectedSize.length) {
      setError("超过 " + formatBytes(maxFileSizeBytes) + "：" + rejectedSize.map((file) => file.name).join("、"));
      return;
    }
    if (picked.length > maxFiles) {
      setError("一次最多上传 " + maxFiles + " 个文件");
      return;
    }
    setError(null);
    onUpload(picked);
  };

  const handlePick = (event: ChangeEvent<HTMLInputElement>) => {
    acceptFiles(Array.from(event.target.files ?? []));
    event.target.value = "";
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    acceptFiles(Array.from(event.dataTransfer.files ?? []));
  };

  return (
    <div className="space-y-2">
      <div
        className={cn(
          CARD,
          "transition-colors",
          dragging && "border-neutral-500 bg-neutral-100",
          disabled && "opacity-60",
        )}
        onDragEnter={(event) => {
          event.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDragOver={(event) => event.preventDefault()}
        onDrop={handleDrop}
      >
        <input
          accept={accept}
          className="hidden"
          disabled={disabled || uploading}
          multiple
          onChange={handlePick}
          ref={inputRef}
          type="file"
        />
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <p className="text-sm font-medium text-neutral-800">拖拽文件到这里，或选择文件</p>
            <p className="mt-1 text-[11px] leading-relaxed text-neutral-500">
              支持 {allowedExtensions.map((ext) => "." + ext).join("、")}；单文件 ≤{" "}
              {formatBytes(maxFileSizeBytes)}，一次最多 {maxFiles} 个。
              文本和 Markdown 直接读取，PDF/Office 等交给 MinerU。
            </p>
          </div>
          <Button
            disabled={disabled || uploading}
            onClick={() => inputRef.current?.click()}
            size="sm"
            type="button"
            variant="primary"
          >
            {uploading ? "上传中…" : "选择文件"}
          </Button>
        </div>
      </div>
      {error ? (
        <p className="text-xs text-red-600" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
