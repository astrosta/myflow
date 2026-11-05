"use client";

import { type ChangeEvent, useEffect, useRef, useState } from "react";

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

const TRIGGER_CLASS = cn(
  "inline-flex h-7 cursor-pointer items-center gap-1 rounded-md border border-neutral-300 bg-white px-2.5",
  "text-[11px] font-medium text-neutral-700 transition-colors",
  "hover:border-neutral-400 hover:text-neutral-900",
);
const MENU_ITEM_CLASS =
  "block w-full px-3 py-1.5 text-left text-[11px] text-neutral-700 transition-colors hover:bg-neutral-100 hover:text-neutral-900";

/**
 * 一个「上传」入口，菜单里区分文件和文件夹。
 *
 * 原生 file input 的 webkitdirectory 是互斥开关：打开就只能选目录、关着就只能选
 * 散文件，浏览器没有"两者皆可"的第三种模式。所以合并的是**入口**：同一颗按钮
 * 弹出两项，分别驱动两个隐藏 input，用户看到的上传是一个控件。
 */
function UploadMenu({
  uploading,
  disabled,
  onUpload,
}: {
  uploading: boolean;
  disabled: boolean;
  onUpload: (files: File[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const folderInputRef = useRef<HTMLInputElement>(null);
  const blocked = uploading || disabled;

  useEffect(() => {
    if (!open) return;
    const closeOnOutside = (event: PointerEvent) => {
      const target = event.target as Node | null;
      if (target && containerRef.current?.contains(target)) return;
      setOpen(false);
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener("pointerdown", closeOnOutside);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutside);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    // 菜单一打开就把焦点送进第一项，键盘用户不用再 Tab 一次。
    menuRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
  }, [open]);

  const select = (event: ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(event.target.files ?? []);
    // 清空 value，连续选择同一个文件/目录也能再次触发 change。
    event.target.value = "";
    if (selected.length > 0) onUpload(selected);
  };

  const pick = (kind: "file" | "folder") => {
    setOpen(false);
    const input = kind === "file" ? fileInputRef.current : folderInputRef.current;
    input?.click();
  };

  return (
    <div className="relative" ref={containerRef}>
      <button
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label="上传"
        className={cn(
          TRIGGER_CLASS,
          blocked && "cursor-not-allowed text-neutral-400 hover:border-neutral-300 hover:text-neutral-400",
        )}
        disabled={blocked}
        onClick={() => setOpen((value) => !value)}
        ref={triggerRef}
        type="button"
      >
        {uploading ? "上传中…" : "上传"}
        <span aria-hidden="true" className={cn("text-[9px] leading-none", open && "rotate-180")}>
          ▾
        </span>
      </button>

      {open ? (
        <div
          aria-label="上传方式"
          className="absolute right-0 z-20 mt-1 w-36 overflow-hidden rounded-md border border-neutral-200 bg-white py-1 shadow-lg"
          ref={menuRef}
          role="menu"
        >
          <button className={MENU_ITEM_CLASS} onClick={() => pick("file")} role="menuitem" type="button">
            选择文件…
          </button>
          <button className={MENU_ITEM_CLASS} onClick={() => pick("folder")} role="menuitem" type="button">
            选择文件夹…
          </button>
        </div>
      ) : null}

      <input
        aria-label="选择文件"
        className="sr-only"
        disabled={blocked}
        multiple
        onChange={select}
        ref={fileInputRef}
        type="file"
      />
      <input
        aria-label="选择文件夹"
        className="sr-only"
        disabled={blocked}
        multiple
        onChange={select}
        ref={(node) => {
          folderInputRef.current = node;
          if (!node) return;
          // React 不认 webkitdirectory/directory 这两个非标准属性，只能命令式设置。
          node.setAttribute("webkitdirectory", "");
          node.setAttribute("directory", "");
        }}
        type="file"
      />
    </div>
  );
}

/**
 * 草稿文件树 + 上传入口。
 *
 * 每个路径是一个原生 button（不是 div + onClick）：键盘可达、有焦点样式，
 * 选中态用 aria-current 告诉读屏用户当前编辑的是哪一份文件。上传是一个入口
 * （按钮 + 菜单，见 UploadMenu），多选或整目录都交给页面去调 uploadDraftFiles；
 * 这里不自己发请求，也不缓存上传结果。
 */
export function SkillFileTree({
  files,
  selectedPath,
  onSelect,
  onUpload,
  uploading = false,
  disabled = false,
}: SkillFileTreeProps) {
  return (
    <aside className="rounded-lg border border-neutral-200 bg-neutral-50/60 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-xs font-medium text-neutral-500">文件</h2>
        <UploadMenu disabled={disabled} onUpload={onUpload} uploading={uploading} />
      </div>

      {files.length === 0 ? (
        <p className="mt-3 text-xs leading-relaxed text-neutral-400">
          还没有文件。点「上传」选择文件或文件夹，也可以直接在右侧编写 SKILL.md。
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
