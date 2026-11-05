"use client";

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";

export interface SkillEditorActionsProps {
  busy: "save" | "scan" | "activate" | "delete" | "upload" | null;
  canSave: boolean;
  canScan: boolean;
  canActivate: boolean;
  onSave: () => void;
  onScan: () => void;
  onActivate: () => void;
  onDelete: () => void;
}

/**
 * 编辑器底部的动作条：保存 → 安全扫描 → 生效 → 删除。
 *
 * 保存只写草稿版本，因此没有未保存修改时禁用；扫描和生效都必须针对已保存
 * 版本，存在未保存修改时保持置灰。删除不可逆，单独走确认对话框；生效会
 * 写入运行时与检索索引，也需要用户确认。
 */
export function SkillEditorActions({
  busy,
  canSave,
  canScan,
  canActivate,
  onSave,
  onScan,
  onActivate,
  onDelete,
}: SkillEditorActionsProps) {
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [confirmingActivate, setConfirmingActivate] = useState(false);
  const idle = busy === null;

  useEffect(() => {
    if (!confirmingDelete && !confirmingActivate) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setConfirmingDelete(false);
        setConfirmingActivate(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirmingActivate, confirmingDelete]);

  return (
    <>
      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-neutral-200 bg-white p-3">
        <Button
          disabled={!idle || !canSave}
          onClick={onSave}
          size="sm"
          title={canSave ? "保存草稿并生成新版本" : "没有未保存的修改"}
          type="button"
          variant="primary"
        >
          {busy === "save" ? "保存中…" : "保存"}
        </Button>
        <Button
          disabled={!idle || !canScan}
          onClick={onScan}
          size="sm"
          title={canScan ? "基于最新已保存版本运行安全扫描" : "请先保存未提交的修改，再运行安全扫描"}
          type="button"
        >
          {busy === "scan" ? "扫描中…" : "安全扫描"}
        </Button>
        <Button
          disabled={!idle || !canActivate}
          onClick={() => setConfirmingActivate(true)}
          size="sm"
          title={canActivate ? "确认后让技能进入运行时并构建索引" : "请先完成并通过安全扫描"}
          type="button"
        >
          {busy === "activate" ? "生效中…" : "生效"}
        </Button>
        <Button
          className="ml-auto"
          disabled={!idle}
          onClick={() => setConfirmingDelete(true)}
          size="sm"
          type="button"
          variant="danger"
        >
          删除技能
        </Button>
      </div>

      {confirmingActivate ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-neutral-900/40 px-4"
          onClick={() => setConfirmingActivate(false)}
          role="presentation"
        >
          <div
            aria-label="确认生效"
            aria-modal="true"
            className="w-full max-w-sm rounded-xl border border-neutral-200 bg-white shadow-xl"
            onClick={(event) => event.stopPropagation()}
            role="dialog"
          >
            <div className="px-5 py-4">
              <h2 className="text-sm font-semibold text-neutral-900">确认生效</h2>
              <p className="mt-2 text-xs leading-relaxed text-neutral-500">
                生效后，最新版本会进入运行时并构建检索索引。确定让这个技能生效吗？
              </p>
            </div>
            <div className="flex justify-end gap-2 border-t border-neutral-100 px-5 py-3">
              <Button onClick={() => setConfirmingActivate(false)} size="sm" type="button">
                取消
              </Button>
              <Button
                onClick={() => {
                  setConfirmingActivate(false);
                  onActivate();
                }}
                size="sm"
                type="button"
                variant="primary"
              >
                确认生效
              </Button>
            </div>
          </div>
        </div>
      ) : null}

      {confirmingDelete ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-neutral-900/40 px-4"
          onClick={() => setConfirmingDelete(false)}
          role="presentation"
        >
          <div
            aria-label="删除技能"
            aria-modal="true"
            className="w-full max-w-sm rounded-xl border border-neutral-200 bg-white shadow-xl"
            onClick={(event) => event.stopPropagation()}
            role="dialog"
          >
            <div className="px-5 py-4">
              <h2 className="text-sm font-semibold text-neutral-900">删除技能</h2>
              <p className="mt-2 text-xs leading-relaxed text-neutral-500">
                技能、暂存文件和运行时索引都会删除，且无法恢复。确定继续吗？
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
