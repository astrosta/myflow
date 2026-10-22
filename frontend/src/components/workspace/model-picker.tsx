"use client";

import { useEffect, useRef, useState } from "react";

import { ChevronDownIcon } from "@/components/ui/icons";
import { modelLabel, type ModelInfo, type ReasoningEffort } from "@/core/api/models";
import { cn } from "@/lib/utils";

/**
 * 输入框右下角的模型 / 推理等级选择器。
 *
 * 交互对齐 DeepSeek Harness 的 composer：**一个紧凑触发器 + 两级菜单**
 * （模型、推理等级各一段），而不是在工具行上摆两个大下拉。理由：
 *
 * - 工具行的高度是固定的，两个 select 会把输入框撑高、把附件和发送挤小；
 * - 触发器一行就能读完当前选择（`模型 · 等级`），菜单里才展开细节；
 * - 推理等级是**模型的属性**：模型没公布候选时整段不出现，而不是给一个空下拉。
 *
 * 目录与候选全部来自 `/api/models`（也就是 config.yaml），组件不认识模型名，
 * 也不猜默认值——默认值由 `selection.ts` 按 config 的 default 标记解析。
 */
export interface ModelPickerProps {
  models: ModelInfo[];
  current: ModelInfo | null;
  effortOptions: ReasoningEffort[];
  effortId: string | null;
  effortLabel: string;
  loading?: boolean;
  error?: string | null;
  disabled?: boolean;
  onSelectModel: (name: string) => void;
  onSelectEffort: (id: string | null) => void;
}

export function ModelPicker({
  models,
  current,
  effortOptions,
  effortId,
  effortLabel,
  loading = false,
  error = null,
  disabled = false,
  onSelectModel,
  onSelectEffort,
}: ModelPickerProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  // Esc 关闭 + 点外面关闭：菜单是浮层，缺了这两条键盘用户会被困住
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onPointerDown = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onPointerDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onPointerDown);
    };
  }, [open]);

  const hasEffort = effortOptions.length > 0;
  const triggerText = current
    ? hasEffort
      ? modelLabel(current) + " · " + effortLabel
      : modelLabel(current)
    : loading
      ? "加载模型…"
      : "选择模型";

  return (
    <div className="relative" ref={rootRef}>
      <button
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label={
          current && hasEffort
            ? "选择模型，当前 " + modelLabel(current) + "，推理等级 " + effortLabel
            : "选择模型"
        }
        className={cn(
          "inline-flex h-8 max-w-[16rem] items-center gap-1 rounded-full px-2.5 text-xs transition-colors",
          "border border-neutral-200 text-neutral-600 hover:border-neutral-300 hover:text-neutral-900",
          disabled && "cursor-not-allowed opacity-50 hover:border-neutral-200 hover:text-neutral-600",
        )}
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <span className="truncate">{triggerText}</span>
        <ChevronDownIcon size={12} />
      </button>

      {open ? (
        <div
          aria-label="模型与推理等级"
          className="absolute bottom-10 right-0 z-20 max-h-[22rem] w-64 overflow-y-auto rounded-xl border border-neutral-200 bg-white p-1.5 shadow-lg"
          role="menu"
        >
          {error ? (
            <p className="px-2 py-1.5 text-[11px] text-red-600">{error}</p>
          ) : null}

          <MenuSection label="模型">
            {models.length ? (
              models.map((model) => (
                <MenuItem
                  checked={model.name === current?.name}
                  key={model.name}
                  label={modelLabel(model)}
                  description={model.description}
                  onSelect={() => {
                    onSelectModel(model.name);
                    setOpen(false);
                  }}
                />
              ))
            ) : (
              <p className="px-2 py-1.5 text-[11px] text-neutral-400">
                {loading ? "加载中…" : "配置里没有可用的模型。"}
              </p>
            )}
          </MenuSection>

          {hasEffort ? (
            <MenuSection label="推理等级">
              <MenuItem
                checked={effortId === null}
                label="默认"
                description="不指定，交给服务端与 provider"
                onSelect={() => {
                  onSelectEffort(null);
                  setOpen(false);
                }}
              />
              {effortOptions.map((option) => (
                <MenuItem
                  checked={option.id === effortId}
                  key={option.id}
                  label={option.label}
                  description={option.description}
                  onSelect={() => {
                    onSelectEffort(option.id);
                    setOpen(false);
                  }}
                />
              ))}
            </MenuSection>
          ) : current && current.supports_reasoning_effort ? (
            <p className="px-2 py-1.5 text-[11px] text-neutral-400">当前模型未提供推理等级。</p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function MenuSection({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="py-1">
      <p className="px-2 pb-1 text-[11px] font-medium text-neutral-400">{label}</p>
      {children}
    </div>
  );
}

function MenuItem({
  label,
  description,
  checked,
  onSelect,
}: {
  label: string;
  description?: string | null;
  checked: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      aria-checked={checked}
      className={cn(
        "flex w-full flex-col items-start gap-0.5 rounded-md px-2 py-1.5 text-left text-xs transition-colors",
        checked ? "bg-neutral-100 text-neutral-900" : "text-neutral-600 hover:bg-neutral-50",
      )}
      onClick={onSelect}
      role="menuitemradio"
      type="button"
    >
      <span className="font-medium">{label}</span>
      {description ? (
        <span className="text-[11px] leading-snug text-neutral-400">{description}</span>
      ) : null}
    </button>
  );
}
