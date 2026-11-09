"use client";

import { useCallback, useState, type KeyboardEvent } from "react";

import { ArrowUpIcon, StopIcon } from "@/components/ui/icons";
import { Textarea } from "@/components/ui/textarea";
import type { ModelInfo, ReasoningEffort } from "@/core/api/models";
import { cn } from "@/lib/utils";

import { FileUpload } from "./file-upload";
import { ModelPicker } from "./model-picker";

/**
 * 输入框 + 发送 + 停止。
 *
 * 布局上是一整个"输入框"（一个带边框的圆角容器），里面分两行：
 * 上面是 textarea（无边框，融进容器），下面一条工具行——附件在左、
 * **发送按钮在右下角**。和 DeepSeek、千问网页端的输入框一致。
 *
 * 两条刻意的行为：
 *
 * 1. **整框在聚焦时高亮**（focus-within）。之前只有 textarea 自己的
 *    focus:border-neutral-300 —— #e5e5e5 到 #d4d4d4 在白底上等于没变，
 *    用户看不出"我点进来了"。现在边框加深 + 一圈淡 ring。
 * 2. **有内容时发送按钮才点亮**：空输入是灰底禁用，一打字变实心深色。
 *
 * 键盘行为（Enter 发送 / Shift+Enter 换行 / 认 isComposing）和提交失败时
 * 保留输入与附件，都在下面的实现里，没有被这次挪位置改掉。
 */
export interface ComposerProps {
  onSubmit: (text: string, files: readonly File[]) => Promise<void> | void;
  onStop?: () => void;
  streaming?: boolean;
  disabled?: boolean;
  /**
   * "hero"  = 对话还没开始，输入框在页面中间；
   * "docked" = 对话进行中，输入框贴在底部。
   *
   * 只改观感，不改行为。
   */
  variant?: "hero" | "docked";
  placeholder?: string;
  className?: string;
  /**
   * 模型 / 推理等级选择条（放在发送按钮左边）。
   *
   * 由容器层注入而不是在这里取数：Composer 是"只接 props"的展示组件，
   * 目录来自 `/api/models`（也就是 config.yaml），只有容器层知道怎么拿。
   * 不传就完全不渲染这块控件（老调用方、测试都不受影响）。
   */
  modelControl?: ModelControlProps;
}

export interface ModelControlProps {
  models: ModelInfo[];
  current: ModelInfo | null;
  effortOptions: ReasoningEffort[];
  effortId: string | null;
  effortLabel: string;
  loading?: boolean;
  error?: string | null;
  /** 某些厂商的 /models 没拉到（列表已回退到配置） */
  vendorErrors?: readonly string[];
  disabled?: boolean;
  onSelectModel: (name: string) => void;
  onSelectEffort: (id: string | null) => void;
}

export function Composer({
  onSubmit,
  onStop,
  streaming = false,
  disabled = false,
  variant = "docked",
  placeholder = "输入消息，Enter 发送，Shift+Enter 换行",
  className,
  modelControl,
}: ComposerProps) {
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hasText = text.trim().length > 0;
  const canSend = hasText && !submitting && !disabled;
  const isHero = variant === "hero";

  /**
   * 提交 = **先把输入框清空，再等 onSubmit**。
   *
   * onSubmit 走的是 SDK 的 `stream.submit`，它的 promise 要等**整轮 run 结束**（甚至
   * 更晚）才 resolve；等它再清空，用户会看到自己刚打的字一直留在输入框里，像是
   * "消息没发出去"。所以先乐观清空，失败再把内容放回去（用户已经重新输入过就不覆盖）。
   */
  const submit = useCallback(async () => {
    const value = text.trim();
    if (!value || submitting || disabled) return;
    const pendingFiles = files;
    setSubmitting(true);
    setError(null);
    setText("");
    setFiles([]);
    try {
      await onSubmit(value, pendingFiles);
    } catch (cause) {
      setText((current) => (current ? current : value));
      setFiles((current) => (current.length ? current : pendingFiles));
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  }, [text, files, submitting, disabled, onSubmit]);

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey) return;
    if (event.nativeEvent.isComposing) return;
    event.preventDefault();
    void submit();
  };

  return (
    <form
      className={cn(
        "bg-white px-4",
        // 贴底时有一条分隔线把它和消息列表分开；居中时它自己就是主角，不要线
        isHero ? "pb-4 pt-0" : "border-t border-neutral-200 py-3",
        className,
      )}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className={cn("mx-auto w-full", isHero ? "max-w-2xl" : "max-w-3xl")}>
        {/**
          * 失败原因**放在输入框正上方、整行显示**。
          *
          * 之前它挤在工具行右侧、还被 truncate 成十六个字宽——发送失败时
          * 用户看到的是"点了没反应"，而不是"网关返回了 404"。
          * 这种"什么都不发生"的静默失败是最难自查的一类。
          */}
        {error ? (
          <p
            className="mb-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs leading-relaxed break-words text-red-700"
            role="alert"
          >
            发送失败：{error}
          </p>
        ) : null}

        <div
          className={cn(
            "flex flex-col rounded-2xl border bg-white transition-colors",
            // 聚焦高亮：边框明显加深 + 一圈淡 ring。整框一起变，
            // 而不是只看 textarea 那一像素的边框
            "border-neutral-300 focus-within:border-neutral-500",
            "focus-within:ring-2 focus-within:ring-neutral-900/10",
            isHero ? "shadow-sm" : "shadow-sm",
          )}
        >
          <Textarea
            aria-label="消息输入框"
            autoGrow
            // 融进外层容器：去掉自己的边框、圆角和背景。
            //
            // focus-visible:outline-none 是必须的：globals.css 里有一条全局的
            // :focus-visible { outline: 2px solid var(--color-accent) }，
            // 而 --color-accent 是 #1f2937 —— 聚焦时光标周围会出现一个近黑色的矩形框。
            // 去掉它不会丢可访问性：外层容器有 focus-within 的边框加深 + ring，
            // 那个才是给用户看的"我点进来了"。
            className="rounded-none border-0 bg-transparent px-4 pb-1 pt-3 focus:outline-none focus-visible:outline-none"
            disabled={disabled}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
            rows={1}
            value={text}
          />

          {/* 工具行：附件在左，发送 / 停止在**框内右下角** */}
          <div className="flex items-center gap-1 px-2 pb-2">
            <FileUpload
              disabled={disabled || submitting}
              files={files}
              onChange={setFiles}
            />

            <div className="ml-auto flex items-center gap-2">
              {/* 模型 / 推理等级：贴着发送按钮左侧，推理等级只在模型公布候选时出现 */}
              {modelControl ? (
                <ModelPicker
                  current={modelControl.current}
                  disabled={disabled || modelControl.disabled}
                  effortId={modelControl.effortId}
                  effortLabel={modelControl.effortLabel}
                  effortOptions={modelControl.effortOptions}
                  error={modelControl.error}
                  loading={modelControl.loading}
                  models={modelControl.models}
                  onSelectEffort={modelControl.onSelectEffort}
                  onSelectModel={modelControl.onSelectModel}
                  vendorErrors={modelControl.vendorErrors}
                />
              ) : null}
              {streaming ? (
                <button
                  aria-label="停止"
                  className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full border border-neutral-300 bg-white text-neutral-700 transition-colors hover:border-neutral-400 hover:text-neutral-900"
                  disabled={!onStop}
                  onClick={onStop}
                  type="button"
                >
                  <StopIcon size={14} />
                </button>
              ) : (
                /* 空输入 → 灰底禁用；一打字 → 实心深色高亮。这个切换就是"能不能发"的信号 */
                <button
                  aria-label="发送"
                  className={cn(
                    "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full transition-all",
                    canSend
                      ? "bg-neutral-900 text-white hover:bg-neutral-700 active:scale-95"
                      : "cursor-not-allowed bg-neutral-200 text-neutral-400",
                  )}
                  disabled={!canSend}
                  type="submit"
                >
                  <ArrowUpIcon size={18} />
                </button>
              )}
            </div>
          </div>
        </div>

        <div className="mt-2 flex items-center gap-2 px-1">
          {isHero ? null : (
            <span className="ml-auto text-[11px] text-neutral-400">
              Enter 发送 · Shift+Enter 换行
            </span>
          )}
        </div>
      </div>
    </form>
  );
}
