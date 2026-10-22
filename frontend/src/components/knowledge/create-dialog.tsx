"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { CloseIcon } from "@/components/ui/icons";
import {
  createKnowledgeBase,
  KNOWLEDGE_TYPES,
  type KnowledgeBase,
  type KnowledgeBaseType,
} from "@/core/api/knowledge";

export interface CreateKnowledgeBaseDialogProps {
  onClose: () => void;
  onCreated: (base: KnowledgeBase) => void;
}

const INPUT_CLASS =
  "h-9 w-full rounded-md border border-neutral-300 bg-white px-3 text-sm text-neutral-900 focus:border-neutral-500 focus:outline-none";

export function CreateKnowledgeBaseDialog({ onClose, onCreated }: CreateKnowledgeBaseDialogProps) {
  const [name, setName] = useState("");
  const [type, setType] = useState<KnowledgeBaseType>("technical");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const submit = async () => {
    if (busy || !name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const created = await createKnowledgeBase({
        name: name.trim(),
        type,
        description: description.trim() || null,
      });
      onCreated(created);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  const selected = KNOWLEDGE_TYPES.find((item) => item.value === type);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-neutral-900/40 px-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        aria-label="新建知识库"
        aria-modal="true"
        className="w-full max-w-lg rounded-xl border border-neutral-200 bg-white shadow-xl"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
      >
        <header className="flex items-center border-b border-neutral-200 px-5 py-3">
          <h2 className="text-sm font-semibold text-neutral-900">新建知识库</h2>
          <button
            aria-label="关闭"
            className="ml-auto rounded p-1 text-neutral-500 hover:bg-neutral-100 hover:text-neutral-900"
            onClick={onClose}
            type="button"
          >
            <CloseIcon size={16} />
          </button>
        </header>

        <form
          className="space-y-5 px-5 py-5"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-neutral-700">名称</span>
            <input
              aria-label="知识库名称"
              autoFocus
              className={INPUT_CLASS}
              disabled={busy}
              maxLength={120}
              onChange={(event) => setName(event.target.value)}
              placeholder="例如：产品文档、生产环境运维手册"
              value={name}
            />
          </label>

          <fieldset className="space-y-2">
            <legend className="text-xs font-medium text-neutral-700">类型</legend>
            <div className="grid gap-2 sm:grid-cols-2">
              {KNOWLEDGE_TYPES.map((item) => (
                <label
                  className={
                    "flex cursor-pointer items-start gap-2 rounded-lg border px-3 py-2.5 transition-colors " +
                    (type === item.value
                      ? "border-neutral-500 bg-neutral-50"
                      : "border-neutral-200 hover:border-neutral-300")
                  }
                  key={item.value}
                >
                  <input
                    checked={type === item.value}
                    className="mt-0.5"
                    disabled={busy}
                    name="knowledge-type"
                    onChange={() => setType(item.value)}
                    type="radio"
                    value={item.value}
                  />
                  <span className="min-w-0">
                    <span className="block text-xs font-medium text-neutral-800">{item.label}</span>
                    <span className="mt-0.5 block text-[11px] leading-relaxed text-neutral-500">
                      {item.description}
                    </span>
                  </span>
                </label>
              ))}
            </div>
            <p className="text-[11px] text-neutral-500">
              {selected?.label}将使用对应分片策略；类型创建后不可修改。
            </p>
          </fieldset>

          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-neutral-700">说明（可选）</span>
            <input
              aria-label="知识库说明"
              className={INPUT_CLASS}
              disabled={busy}
              maxLength={2000}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="这个知识库主要放什么内容"
              value={description}
            />
          </label>

          {error ? (
            <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700" role="alert">
              {error}
            </p>
          ) : null}

          <div className="flex justify-end gap-2 border-t border-neutral-100 pt-4">
            <Button disabled={busy} onClick={onClose} size="sm" type="button">
              取消
            </Button>
            <Button disabled={busy || !name.trim()} size="sm" type="submit" variant="primary">
              {busy ? "创建中…" : "创建"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
