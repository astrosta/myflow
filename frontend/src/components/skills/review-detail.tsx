"use client";

import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  approveReview,
  getReview,
  rejectReview,
  SkillApiError,
} from "@/core/api/skills";

import { SkillFileTree } from "./skill-file-tree";

/**
 * 一条待审提交的详情：作者、版本、提交 commit、暂存文件树、SKILL.md、diff、
 * 包大小，以及通过 / 驳回两个动作。
 *
 * **通过和驳回都以服务端为准**：这里不判断"当前用户是不是管理员"——能不能审
 * 是后端 admin scope 的事，前端只负责在失败时把 code/detail 如实说出来。
 *
 * 审核完通知队列（onResolved）：队列负责失效 ["skills","reviews"] 并收起详情，
 * 这一层不碰列表的缓存，免得两处都在改同一份数据。
 */

/** 接口错误给一句人读的话：网关的 detail 最具体，没有就退回 status。 */
function readableError(error: unknown): string {
  if (error instanceof SkillApiError) {
    return error.detail || "请求失败（HTTP " + error.status + "）";
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

/** 字节数写成人能一眼读懂的尺寸（和文件树同一个口径）。 */
function formatBytes(bytes: number): string {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

const SECTION_TITLE_CLASS = "text-xs font-medium text-neutral-500";
const PRE_CLASS =
  "thin-scrollbar mt-2 max-h-72 overflow-auto rounded-lg border border-neutral-200 bg-neutral-50 px-3 py-2 font-mono text-[11px] leading-relaxed text-neutral-800 whitespace-pre-wrap";

export interface ReviewDetailProps {
  submissionId: string;
  /** 审完（通过或驳回成功）之后回调，队列据此收起详情并刷新列表。 */
  onResolved?: () => void;
}

export function ReviewDetail({ submissionId, onResolved }: ReviewDetailProps) {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["skills", "review", submissionId],
    queryFn: () => getReview(submissionId),
    staleTime: 5_000,
  });

  const [selectedPath, setSelectedPath] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const [error, setError] = useState<string | null>(null);

  const detail = query.data;

  /**
   * 审核成功后要失效两份服务端状态：
   * - ["skills","reviews"]：队列本身（这一条不该再出现）
   * - ["skills","public"]：通过之后目录快照变了，作者/其他人都会看新版本
   */
  const settled = async () => {
    await queryClient.invalidateQueries({ queryKey: ["skills", "reviews"] });
    await queryClient.invalidateQueries({ queryKey: ["skills", "public"] });
    onResolved?.();
  };

  const approve = async () => {
    if (busy) return;
    setBusy("approve");
    setError(null);
    try {
      await approveReview(submissionId);
      await settled();
    } catch (cause) {
      setError(readableError(cause));
    } finally {
      setBusy(null);
    }
  };

  /** 驳回必须给理由：空的理由等于把作者晾在原地，后端也会拒；这里先拦一次。 */
  const reject = async () => {
    if (busy) return;
    const trimmed = reason.trim();
    if (!trimmed) {
      setError("驳回必须填写理由：告诉作者要改什么，他才知道下一步。");
      return;
    }
    setBusy("reject");
    setError(null);
    try {
      await rejectReview(submissionId, trimmed);
      await settled();
    } catch (cause) {
      setError(readableError(cause));
    } finally {
      setBusy(null);
    }
  };

  if (query.isLoading) {
    return (
      <div aria-busy="true" aria-label="加载提交详情" className="space-y-2 rounded-lg border border-neutral-200 bg-white p-4">
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-24 w-full" />
      </div>
    );
  }

  if (query.error) {
    return (
      <p
        className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700"
        role="alert"
      >
        {readableError(query.error)}
      </p>
    );
  }

  if (!detail) return null;

  return (
    <section
      aria-label={"提交详情 " + detail.name}
      className="rounded-lg border border-neutral-200 bg-white p-4"
    >
      <header className="border-b border-neutral-100 pb-3">
        <h2 className="text-sm font-semibold text-neutral-900">{detail.name}</h2>
        <dl className="mt-2 grid gap-x-6 gap-y-1 text-[11px] text-neutral-500 sm:grid-cols-2">
          <div className="flex gap-1.5">
            <dt>作者</dt>
            <dd className="font-mono text-neutral-700">{detail.owner_user_id}</dd>
          </div>
          <div className="flex gap-1.5">
            <dt>版本</dt>
            <dd className="font-mono text-neutral-700">v{detail.version}</dd>
          </div>
          <div className="flex gap-1.5">
            <dt>提交 commit</dt>
            {/* 短哈希给人读，完整哈希挂在 title 上：排查时还要拿它去仓库里找 */}
            <dd className="font-mono text-neutral-700" title={detail.submitted_commit}>
              {detail.submitted_commit.slice(0, 12)}
            </dd>
          </div>
          <div className="flex gap-1.5">
            <dt>包大小</dt>
            <dd className="font-mono text-neutral-700">{formatBytes(detail.package_bytes)}</dd>
          </div>
        </dl>
      </header>

      <div className="mt-4 grid gap-4 lg:grid-cols-[minmax(0,14rem)_minmax(0,1fr)]">
        {/**
         * 暂存文件树复用编辑器的那个组件（同一份路径/尺寸的呈现）。
         * 审核只读：上传入口禁用，不在这里改作者的任何文件。
         */}
        <SkillFileTree
          disabled
          files={detail.files}
          onSelect={setSelectedPath}
          onUpload={() => {}}
          selectedPath={selectedPath}
        />

        <div className="min-w-0">
          <h3 className={SECTION_TITLE_CLASS}>SKILL.md</h3>
          {detail.skill_md ? (
            <pre className={PRE_CLASS}>{detail.skill_md}</pre>
          ) : (
            <p className="mt-2 text-xs text-neutral-400">这个版本没有 SKILL.md。</p>
          )}

          <h3 className={SECTION_TITLE_CLASS + " mt-4"}>变更</h3>
          {detail.diff ? (
            <pre className={PRE_CLASS}>{detail.diff}</pre>
          ) : (
            <p className="mt-2 text-xs text-neutral-400">和上一版相比没有变更。</p>
          )}
        </div>
      </div>

      {error ? (
        <p
          className="mt-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
          role="alert"
        >
          {error}
        </p>
      ) : null}

      <div className="mt-4 flex flex-col gap-2 border-t border-neutral-100 pt-3 sm:flex-row sm:items-end">
        <label className="min-w-0 flex-1 space-y-1.5">
          <span className="block text-xs font-medium text-neutral-700">驳回理由</span>
          <input
            aria-label="驳回理由"
            className="h-9 w-full rounded-md border border-neutral-300 bg-white px-3 text-sm text-neutral-900 focus:border-neutral-500 focus:outline-none"
            disabled={busy !== null}
            onChange={(event) => setReason(event.target.value)}
            placeholder="写清楚要改什么，作者据此修改后再提交"
            value={reason}
          />
        </label>
        <div className="flex shrink-0 gap-2">
          <Button
            disabled={busy !== null}
            onClick={() => void reject()}
            size="sm"
            type="button"
            variant="danger"
          >
            {busy === "reject" ? "驳回中…" : "驳回"}
          </Button>
          <Button
            disabled={busy !== null}
            onClick={() => void approve()}
            size="sm"
            type="button"
            variant="primary"
          >
            {busy === "approve" ? "通过中…" : "通过"}
          </Button>
        </div>
      </div>
    </section>
  );
}
