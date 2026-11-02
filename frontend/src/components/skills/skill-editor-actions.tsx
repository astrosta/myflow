"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";

export interface SkillEditorActionsProps {
  pending: boolean;
  busy: "save" | "submit" | "withdraw" | "delete" | null;
  onSave: () => void;
  onSubmit: () => void;
  onWithdraw: () => void;
  onDelete: () => void;
}

/**
 * 编辑器底部的动作条。删除是唯一不可逆的动作，所以单独走确认对话框；
 * 其余按钮的忙碌态由页面统一管理，避免两个请求同时在飞。
 *
 * 审核中时保存和提交都禁用：后端要求先撤回，前端提前把路堵上，别让用户
 * 点完才看到 409。删除也跳过确认框，由页面提示先撤回；撤回只在这个状态下出现。
 */
export function SkillEditorActions({
  pending,
  busy,
  onSave,
  onSubmit,
  onWithdraw,
  onDelete,
}: SkillEditorActionsProps) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const idle = busy === null;

  useEffect(() => {
    if (!confirmingDelete) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setConfirmingDelete(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirmingDelete]);

  return (
    <>
      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-neutral-200 bg-white p-3">
        <Button
          disabled={!idle || pending}
          onClick={onSave}
          size="sm"
          type="button"
          variant="primary"
        >
          {busy === "save" ? "保存中…" : "保存"}
        </Button>
        <Button
          disabled={!idle || pending}
          onClick={onSubmit}
          size="sm"
          type="button"
        >
          {busy === "submit" ? "提交中…" : "提交审核"}
        </Button>
        {pending ? (
          <Button
            disabled={!idle}
            onClick={onWithdraw}
            size="sm"
            type="button"
          >
            {busy === "withdraw" ? "撤回中…" : "撤回"}
          </Button>
        ) : null}
        <Button
          className="ml-auto"
          disabled={!idle}
          onClick={() => {
            if (pending) {
              onDelete();
              return;
            }
            setConfirmingDelete(true);
          }}
          size="sm"
          title={pending ? "审核中，请先撤回审核再删除草稿" : undefined}
          type="button"
          variant="danger"
        >
          删除草稿
        </Button>
      </div>

      {confirmingDelete ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-neutral-900/40 px-4"
          onClick={() => setConfirmingDelete(false)}
          role="presentation"
        >
          <div
            aria-label="删除草稿"
            aria-modal="true"
            className="w-full max-w-sm rounded-xl border border-neutral-200 bg-white shadow-xl"
            onClick={(event) => event.stopPropagation()}
            role="dialog"
          >
            <div className="px-5 py-4">
              <h2 className="text-sm font-semibold text-neutral-900">删除草稿</h2>
              <p className="mt-2 text-xs leading-relaxed text-neutral-500">
                草稿和其中的附件都会删除，且无法恢复。确定继续吗？
              </p>
            </div>
            <div className="flex justify-end gap-2 border-t border-neutral-100 px-5 py-3">
              <Button onClick={() => setConfirmingDelete(false)} size="sm" type="button">
                取消
              </Button>
              <Button
                disabled={busy === "delete"}
                onClick={() => {
                  setConfirmingDelete(false);
                  onDelete();
                }}
                size="sm"
                type="button"
                variant="danger"
              >
                {busy === "delete" ? "删除中…" : "确认删除"}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
