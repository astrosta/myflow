"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { CloseIcon } from "@/components/ui/icons";
import { createSkill, type SkillListItem } from "@/core/api/skills";

export interface CreateSkillDialogProps {
  onClose: () => void;
  /** 建好之后交给页面：失效列表 + 跳编辑器都在那一层（和知识库新建保持一致）。 */
  onCreated: (skill: SkillListItem) => void;
}

const INPUT_CLASS =
  "h-9 w-full rounded-md border border-neutral-300 bg-white px-3 text-sm text-neutral-900 focus:border-neutral-500 focus:outline-none";

/**
 * 新建技能草稿。只有名称和说明：正文（SKILL.md、附件）在编辑器里写，
 * 这里多一个字段就等于多一份要同步的初始状态。
 *
 * 键盘行为照抄知识库新建对话框：Escape 关闭、点遮罩关闭、按钮只在有名字时可点。
 */
export function CreateSkillDialog({ onClose, onCreated }: CreateSkillDialogProps) {
  const [name, setName] = useState("");
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
      const created = await createSkill({
        name: name.trim(),
        description: description.trim(),
      });
      onCreated(created);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-neutral-900/40 px-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        aria-label="新建技能"
        aria-modal="true"
        className="w-full max-w-lg rounded-xl border border-neutral-200 bg-white shadow-xl"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
      >
        <header className="flex items-center border-b border-neutral-200 px-5 py-3">
          <h2 className="text-sm font-semibold text-neutral-900">新建技能</h2>
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
              aria-label="技能名称"
              autoFocus
              className={INPUT_CLASS}
              disabled={busy}
              maxLength={120}
              onChange={(event) => setName(event.target.value)}
              placeholder="例如：周报助手、接口巡检"
              value={name}
            />
          </label>

          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-neutral-700">说明（可选）</span>
            <input
              aria-label="技能说明"
              className={INPUT_CLASS}
              disabled={busy}
              maxLength={2000}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="这个技能解决什么问题、什么时候该用"
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
