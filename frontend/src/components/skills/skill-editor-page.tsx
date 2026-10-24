"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import {
  deleteSkill,
  getDraft,
  listDraftFiles,
  readDraftFile,
  saveDraft,
  SkillApiError,
  submitSkill,
  uploadDraftFiles,
  withdrawSkill,
  writeDraftFile,
  type SkillVersion,
} from "@/core/api/skills";

import { SkillEditorActions } from "./skill-editor-actions";
import { SkillFileTree } from "./skill-file-tree";
import { SkillStatusBadge } from "./skill-status-badge";

export interface SkillEditorPageProps {
  skillId: string;
}

/** 所有草稿相关查询都挂在这个前缀下，一次失效就把树、正文、pending 一起刷新。 */
const draftKey = (skillId: string) => ["skills", "draft", skillId] as const;

type Busy = "save" | "submit" | "withdraw" | "delete" | "upload" | null;

/** 接口错误给一句人读的话；409 是审核流程里最常见的一种，单独说清楚。 */
function readableError(error: unknown): string {
  if (error instanceof SkillApiError) {
    if (error.status === 409) {
      return error.detail || "已有待审核的提交，请先撤回后再提交";
    }
    return error.detail || "请求失败（HTTP " + error.status + "）";
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

function MetaItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] text-neutral-400">{label}</dt>
      <dd className="mt-0.5 truncate font-mono text-xs text-neutral-700" title={value}>
        {value}
      </dd>
    </div>
  );
}

/**
 * 私有技能草稿编辑器。
 *
 * 取数分三层：getDraft（技能行 + pending）、listDraftFiles（文件树）、
 * readDraftFile（当前选中的文件）。正文和选中路径都是**本地状态**，用户不点
 * 保存就永远不写后端——自动保存会让"改坏了想撤销"变成不可能。
 *
 * 动作成功后统一失效 ["skills","mine"] 和 ["skills","draft",skill_id] 前缀：
 * 列表页看到的状态、pending、文件树都来自这两个缓存。
 */
export function SkillEditorPage({ skillId }: SkillEditorPageProps) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [selectedPath, setSelectedPath] = useState("SKILL.md");
  // 本地编辑区只在"用户改过当前文件"时存在：服务端内容到达时由下文的 text 派生，
  // 不在 effect 里 setState——那会触发级联渲染，而且用户切文件时会把旧的本地
  // 内容盖回去。不点保存就永远不写后端。
  const [edit, setEdit] = useState<{ path: string; text: string } | null>(null);
  const [lastVersion, setLastVersion] = useState<SkillVersion | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const draftQuery = useQuery({
    queryKey: draftKey(skillId),
    queryFn: () => getDraft(skillId),
  });
  const filesQuery = useQuery({
    queryKey: [...draftKey(skillId), "files"],
    queryFn: () => listDraftFiles(skillId),
  });
  const fileQuery = useQuery({
    queryKey: [...draftKey(skillId), "file", selectedPath],
    queryFn: () => readDraftFile(skillId, selectedPath),
    enabled: selectedPath.length > 0,
  });

  // 编辑区正文：改过就用本地那份，否则直接用服务端读回来的。
  const serverText = fileQuery.data?.content ?? "";
  const text = edit?.path === selectedPath ? edit.text : serverText;
  const dirty = text !== serverText;
  const setText = (next: string) => setEdit({ path: selectedPath, text: next });

  const draft = draftQuery.data;
  const files = filesQuery.data ?? draft?.files ?? [];
  const pending = draft?.pending ?? false;
  const pageError = draftQuery.error ?? filesQuery.error;
  const loading = draftQuery.isLoading || filesQuery.isLoading;

  const invalidateDraft = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["skills", "mine"] }),
      queryClient.invalidateQueries({ queryKey: draftKey(skillId) }),
    ]);
  };

  const begin = (kind: Exclude<Busy, null>) => {
    setBusy(kind);
    setActionError(null);
    setNotice(null);
  };

  const selectPath = (path: string) => {
    setSelectedPath(path);
    // 换文件 = 放弃上一份未保存的本地内容（不静默写盘）。
    setEdit(null);
    setActionError(null);
  };

  const save = async () => {
    if (busy || pending) return;
    begin("save");
    try {
      await writeDraftFile(skillId, { path: selectedPath, content: text });
      setEdit(null);
      setLastVersion(await saveDraft(skillId));
      await invalidateDraft();
      setNotice("已保存");
    } catch (cause) {
      setActionError(readableError(cause));
    } finally {
      setBusy(null);
    }
  };

  const upload = async (selected: File[]) => {
    if (busy) return;
    begin("upload");
    try {
      await uploadDraftFiles(skillId, selected);
      await invalidateDraft();
      setNotice("已上传 " + selected.length + " 个文件");
    } catch (cause) {
      setActionError(readableError(cause));
    } finally {
      setBusy(null);
    }
  };

  const submit = async () => {
    if (busy || pending) return;
    begin("submit");
    try {
      await submitSkill(skillId);
      await invalidateDraft();
      setNotice("已提交审核");
    } catch (cause) {
      setActionError(readableError(cause));
    } finally {
      setBusy(null);
    }
  };

  const withdraw = async () => {
    if (busy || !pending) return;
    begin("withdraw");
    try {
      await withdrawSkill(skillId);
      await invalidateDraft();
      setNotice("已撤回，可以继续编辑");
    } catch (cause) {
      setActionError(readableError(cause));
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    if (busy) return;
    begin("delete");
    try {
      await deleteSkill(skillId);
      await queryClient.invalidateQueries({ queryKey: ["skills", "mine"] });
      router.push("/workspace/skills");
    } catch (cause) {
      setActionError(readableError(cause));
      setBusy(null);
    }
  };

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto w-full max-w-6xl px-5 py-6 sm:px-8">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-neutral-200 pb-5">
          <div className="min-w-0">
            <Link
              className="text-[11px] text-neutral-500 hover:text-neutral-900"
              href="/workspace/skills"
            >
              ← 返回技能列表
            </Link>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <h1 className="truncate text-xl font-semibold tracking-tight text-neutral-900">
                {draft?.skill.name || "技能编辑器"}
              </h1>
              {draft ? <SkillStatusBadge status={draft.skill.status} /> : null}
            </div>
            <p className="mt-1 text-xs leading-relaxed text-neutral-500">
              私有草稿只有你自己能看到；保存到版本后可以提交审核。
            </p>
          </div>
        </header>

        {loading ? (
          <div className="mt-5 space-y-3" aria-busy="true" aria-label="加载技能草稿">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-96 w-full" />
          </div>
        ) : null}

        {pageError ? (
          <p
            className="mt-5 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700"
            role="alert"
          >
            {readableError(pageError)}
          </p>
        ) : null}

        {draft && !pageError ? (
          <>
            <dl className="mt-5 grid grid-cols-2 gap-3 rounded-lg border border-neutral-200 bg-neutral-50/60 px-4 py-3 sm:grid-cols-4">
              <MetaItem
                label="版本"
                value={lastVersion?.version_id ?? draft.skill.version_id ?? "—"}
              />
              <MetaItem
                label="草稿提交"
                value={lastVersion?.draft_commit ?? draft.skill.git_commit ?? "—"}
              />
              <MetaItem label="索引状态" value={lastVersion?.index_status ?? "—"} />
              <div>
                <dt className="text-[11px] text-neutral-400">审核状态</dt>
                <dd className="mt-0.5">
                  <SkillStatusBadge status={draft.skill.status} />
                </dd>
              </div>
            </dl>

            {draft.skill.reject_reason ? (
              <p
                className="mt-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700"
                role="status"
              >
                驳回原因：{draft.skill.reject_reason}
              </p>
            ) : null}

            {pending ? (
              <p
                className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-800"
                role="status"
              >
                审核中，请先撤回
              </p>
            ) : null}

            {actionError ? (
              <p
                className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
                role="alert"
              >
                {actionError}
              </p>
            ) : null}

            {notice ? (
              <p
                className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700"
                role="status"
              >
                {notice}
              </p>
            ) : null}

            <div className="mt-5 grid gap-4 lg:grid-cols-[15rem_minmax(0,1fr)]">
              <SkillFileTree
                disabled={busy !== null}
                files={files}
                onSelect={selectPath}
                onUpload={(selected) => void upload(selected)}
                selectedPath={selectedPath}
                uploading={busy === "upload"}
              />

              <section className="min-w-0 rounded-lg border border-neutral-200 bg-white p-3">
                <div className="flex items-center justify-between gap-2">
                  <h2 className="truncate font-mono text-xs font-medium text-neutral-700">
                    {selectedPath}
                  </h2>
                  {fileQuery.isFetching ? (
                    <span className="text-[11px] text-neutral-400">加载中…</span>
                  ) : dirty ? (
                    <span className="text-[11px] text-amber-700">未保存的修改</span>
                  ) : null}
                </div>

                {fileQuery.error ? (
                  <p
                    className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
                    role="alert"
                  >
                    {readableError(fileQuery.error)}
                  </p>
                ) : fileQuery.isLoading ? (
                  <Skeleton className="mt-3 h-80 w-full" />
                ) : (
                  <label className="mt-2 block" htmlFor="skill-file-content">
                    <span className="sr-only">文件内容</span>
                    <Textarea
                      aria-label="文件内容"
                      className="min-h-[24rem] font-mono text-xs"
                      disabled={pending}
                      id="skill-file-content"
                      onChange={(event) => setText(event.target.value)}
                      spellCheck={false}
                      value={text}
                    />
                  </label>
                )}

                {pending ? (
                  <p className="mt-2 text-[11px] text-neutral-500">
                    审核中的提交不能修改；撤回后才能继续编辑。
                  </p>
                ) : null}

                <div className="mt-3">
                  <SkillEditorActions
                    busy={busy === "upload" ? null : busy}
                    onDelete={() => void remove()}
                    onSave={() => void save()}
                    onSubmit={() => void submit()}
                    onWithdraw={() => void withdraw()}
                    pending={pending}
                  />
                </div>
              </section>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
