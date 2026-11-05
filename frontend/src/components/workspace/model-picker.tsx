"use client";

import { useEffect, useRef, useState } from "react";

import { ChevronDownIcon } from "@/components/ui/icons";
import { modelLabel, type ModelInfo, type ReasoningEffort } from "@/core/api/models";
import { groupByProvider } from "@/core/models/selection";
import { cn } from "@/lib/utils";

/**
 * 输入框右下角的模型 / 推理等级选择器（样式参考 DeepSeek Harness 的 composer）。
 *
 * 三层：
 *
 * ```text
 * 触发器（Flash · Off）
 *   └─ 根菜单：模型 / 推理等级 两行（各显示当前值）
 *        ├─ 模型列表：厂商小字 + 该厂商的模型，当前项打勾
 *        └─ 推理等级列表：Off / Low / Medium / High，当前项打勾
 * ```
 *
 * 候选全部来自 `/api/models`（网关带着配置里的 key 去调厂商 `/models`）；
 * 组件不认识模型名，也不猜默认值：预选哪一档由后端的 `default_reasoning_effort` 决定。
 * 文案保持"名称 + 勾"，不写解释性中文描述。
 */
export interface ModelPickerProps {
  models: ModelInfo[];
  current: ModelInfo | null;
  effortOptions: ReasoningEffort[];
  effortId: string | null;
  effortLabel: string;
  /** 模型支持思考时，根菜单显示「思考」开关 */
  thinkingSupported?: boolean;
  thinkingEnabled?: boolean;
  onToggleThinking?: () => void;
  loading?: boolean;
  error?: string | null;
  vendorErrors?: readonly string[];
  disabled?: boolean;
  onSelectModel: (name: string) => void;
  onSelectEffort: (id: string | null) => void;
}

type PickerView = "root" | "models" | "efforts";

/** Off = 不指定等级，交给服务端与 provider 的默认值。 */
export const NO_EFFORT_LABEL = "Off";

export function ModelPicker({
  models,
  current,
  effortOptions,
  effortId,
  effortLabel: currentEffortLabel,
  thinkingSupported = false,
  thinkingEnabled = false,
  onToggleThinking,
  loading = false,
  error = null,
  vendorErrors = [],
  disabled = false,
  onSelectModel,
  onSelectEffort,
}: ModelPickerProps) {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<PickerView>("root");
  const rootRef = useRef<HTMLDivElement | null>(null);

  // Esc 关闭 + 点外面关闭：浮层缺了这两条，键盘用户会被困住
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

  const toggle = () => {
    setView("root");
    setOpen((value) => !value);
  };

  const hasEffort = effortOptions.length > 0;
  const effortText = hasEffort ? currentEffortLabel : NO_EFFORT_LABEL;
  const triggerText = current
    ? hasEffort
      ? modelLabel(current) + " · " + effortText
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
            ? "选择模型，当前 " + modelLabel(current) + "，推理等级 " + effortText
            : "选择模型"
        }
        className={cn(
          "inline-flex h-8 max-w-[16rem] items-center gap-1.5 rounded-full px-3 font-sans text-xs font-medium",
          "border border-neutral-200 text-neutral-700 transition-colors",
          "hover:border-neutral-300 hover:text-neutral-900",
          disabled && "cursor-not-allowed opacity-50 hover:border-neutral-200 hover:text-neutral-700",
        )}
        disabled={disabled}
        onClick={toggle}
        type="button"
      >
        <span className="truncate">{triggerText}</span>
        <ChevronDownIcon size={12} />
      </button>

      {open ? (
        <div
          aria-label={VIEW_LABELS[view]}
          className="absolute bottom-10 right-0 z-20 max-h-[22rem] w-64 overflow-y-auto rounded-xl border border-neutral-200 bg-white p-2 font-sans shadow-lg"
          role="menu"
        >
          {error ? <p className="px-2 py-1 text-[11px] text-red-600">{error}</p> : null}

          {view === "root" ? (
            <RootMenu
              effortText={effortText}
              hasEffort={hasEffort}
              modelLabelText={current ? modelLabel(current) : "选择模型"}
              onOpenEfforts={() => setView("efforts")}
              onOpenModels={() => setView("models")}
              thinkingEnabled={thinkingEnabled}
              thinkingSupported={thinkingSupported}
              onToggleThinking={
                onToggleThinking
                  ? () => {
                      onToggleThinking();
                      setOpen(false);
                    }
                  : undefined
              }
            />
          ) : view === "models" ? (
            <ModelList
              current={current}
              loading={loading}
              models={models}
              onBack={() => setView("root")}
              onSelect={(name) => {
                onSelectModel(name);
                setOpen(false);
              }}
              vendorErrors={vendorErrors}
            />
          ) : (
            <EffortList
              effortId={effortId}
              effortOptions={effortOptions}
              onBack={() => setView("root")}
              onSelect={(id) => {
                onSelectEffort(id);
                setOpen(false);
              }}
            />
          )}
        </div>
      ) : null}
    </div>
  );
}

const VIEW_LABELS: Record<PickerView, string> = {
  root: "模型与推理等级",
  models: "模型列表",
  efforts: "推理等级列表",
};

/** 根菜单：模型 / 推理等级两行，各显示当前值，点进去再看列表。 */
function RootMenu({
  modelLabelText,
  effortText,
  hasEffort,
  onOpenModels,
  onOpenEfforts,
  thinkingSupported,
  thinkingEnabled,
  onToggleThinking,
}: {
  modelLabelText: string;
  effortText: string;
  hasEffort: boolean;
  onOpenModels: () => void;
  onOpenEfforts: () => void;
  thinkingSupported: boolean;
  thinkingEnabled: boolean;
  onToggleThinking?: () => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <EntryRow label="模型" onClick={onOpenModels} value={modelLabelText} />
      {hasEffort ? (
        <EntryRow label="推理等级" onClick={onOpenEfforts} value={effortText} />
      ) : null}
      {thinkingSupported && onToggleThinking ? (
        <EntryRow
          label="思考"
          onClick={onToggleThinking}
          value={thinkingEnabled ? "开" : "关"}
        />
      ) : null}
    </div>
  );
}

/** 根菜单的一行：左侧名称、右侧当前值、箭头。 */
function EntryRow({
  label,
  value,
  onClick,
}: {
  label: string;
  value: string;
  onClick: () => void;
}) {
  return (
    <button
      aria-label={label}
      className="flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left text-xs text-neutral-600 transition-colors hover:bg-neutral-50 hover:text-neutral-900"
      onClick={onClick}
      role="menuitem"
      type="button"
    >
      <span className="font-medium text-neutral-800">{label}</span>
      <span className="ml-auto truncate text-neutral-400">{value}</span>
      <ChevronDownIcon className="-rotate-90 text-neutral-300" size={12} />
    </button>
  );
}

/** 模型列表：厂商小字 + 模型行（当前项打勾）。 */
function ModelList({
  models,
  current,
  loading,
  vendorErrors,
  onSelect,
  onBack,
}: {
  models: ModelInfo[];
  current: ModelInfo | null;
  loading: boolean;
  vendorErrors: readonly string[];
  onSelect: (name: string) => void;
  onBack: () => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <BackRow label="模型" onBack={onBack} />
      {models.length ? (
        groupByProvider(models).map((group) => (
          <div key={group.provider} className="flex flex-col gap-0.5">
            {/* 厂商名：模型列表上方的小字 */}
            <p className="px-2.5 pb-1 pt-1.5 font-sans text-[11px] font-medium tracking-wide text-neutral-400">
              {group.provider}
            </p>
            {group.models.map((model) => (
              <OptionRow
                key={model.name}
                checked={model.name === current?.name}
                label={modelLabel(model)}
                onSelect={() => onSelect(model.name)}
              />
            ))}
          </div>
        ))
      ) : (
        <p className="px-2.5 py-1.5 text-[11px] text-neutral-400">
          {loading ? "加载中…" : "配置里没有可用的模型。"}
        </p>
      )}

      {vendorErrors.length ? (
        <p className="px-2.5 py-1 text-[11px] text-neutral-400">
          部分厂商列表不可用：{vendorErrors.join("、")}
        </p>
      ) : null}
    </div>
  );
}

/** 推理等级列表：Off + 模型公布的候选（当前项打勾）。 */
function EffortList({
  effortOptions,
  effortId,
  onSelect,
  onBack,
}: {
  effortOptions: ReasoningEffort[];
  effortId: string | null;
  onSelect: (id: string | null) => void;
  onBack: () => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <BackRow label="推理等级" onBack={onBack} />
      <OptionRow
        checked={effortId === null}
        label={NO_EFFORT_LABEL}
        onSelect={() => onSelect(null)}
      />
      {effortOptions.map((option) => (
        <OptionRow
          key={option.id}
          checked={option.id === effortId}
          label={effortOptionLabel(option)}
          onSelect={() => onSelect(option.id)}
        />
      ))}
    </div>
  );
}

/** 等级名原样显示（配置里写什么就是什么）；只有"不指定"固定叫 Off。 */
function effortOptionLabel(option: ReasoningEffort): string {
  return option.label?.trim() || option.id;
}

function BackRow({ label, onBack }: { label: string; onBack: () => void }) {
  return (
    <button
      aria-label={"返回" + label}
      className="flex items-center gap-1 rounded-lg px-2 py-1.5 text-left text-[11px] text-neutral-400 transition-colors hover:text-neutral-700"
      onClick={onBack}
      type="button"
    >
      <ChevronDownIcon className="rotate-90" size={12} />
      {label}
    </button>
  );
}

/** 一行可选：名称在左，选中项右侧打勾。 */
function OptionRow({
  label,
  checked,
  onSelect,
}: {
  label: string;
  checked: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      aria-checked={checked}
      className={cn(
        "flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left font-sans text-xs font-medium transition-colors",
        checked ? "bg-neutral-100 text-neutral-900" : "text-neutral-700 hover:bg-neutral-50",
      )}
      onClick={onSelect}
      role="menuitemradio"
      type="button"
    >
      <span className="truncate">{label}</span>
      <span aria-hidden className="ml-auto shrink-0 text-neutral-900">
        {checked ? "✓" : ""}
      </span>
    </button>
  );
}


