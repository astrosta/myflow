"use client";

import Link from "next/link";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { BookOpenIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import { listReviews, SkillApiError } from "@/core/api/skills";
import { formatTimestamp } from "@/lib/utils";

import { ReviewDetail } from "./review-detail";

/**
 * 管理端的技能审核队列：左边是待审提交的列表，右边（选中之后）是这一条的详情。
 *
 * **列表和详情放在同一个页面**，不是两个路由：审核是"看一眼 → 通过/驳回"的
 * 连续动作，翻页跳到新路由会把队列位置、滚动位置都丢掉。详情入口仍然是真
 * <Link>（href 里带 submission_id，中键/复制链接都有意义），点击时拦掉导航、
 * 就地展开——单页里没有第二个路由可以落。
 *
 * **这里不做"当前用户能不能审"的判断。** 真正的闸门是后端 /admin 路由上的
 * admin scope；前端把入口藏起来只是别让普通用户点进一个只会 403 的页面。
 * 服务端照旧会拒绝，见 review-detail 里对失败的处理。
 */

/** 接口错误给一句人读的话：网关的 detail 最具体，没有就退回 status。 */
function readableError(error: unknown): string {
  if (error instanceof SkillApiError) {
    return error.detail || "请求失败（HTTP " + error.status + "）";
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

/** 详情入口的 href：同一页面的锚，带上 submission_id 便于分享/回看。 */
const detailHref = (submissionId: string) =>
  "/workspace/admin/skills?review=" + encodeURIComponent(submissionId);

export function ReviewQueuePage() {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const query = useQuery({
    queryKey: ["skills", "reviews"],
    queryFn: listReviews,
    staleTime: 5_000,
  });

  const items = query.data ?? [];
  const loading = query.isLoading;
  const empty = !loading && !query.error && items.length === 0;

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto w-full max-w-5xl px-5 py-6 sm:px-8">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-neutral-200 pb-5">
          <div>
            <div className="flex items-center gap-2">
              <BookOpenIcon className="text-neutral-500" size={18} />
              <h1 className="text-xl font-semibold tracking-tight text-neutral-900">技能审核</h1>
            </div>
            <p className="mt-1 text-xs leading-relaxed text-neutral-500">
              通过之后这一版进入公共目录，所有人可见；驳回要写清楚理由，作者改完可以再提交。
            </p>
          </div>
          <div className="flex items-center gap-3">
            {/**
             * 待审数量：给的是"手上还有几件事"，放在刷新按钮左边、不加边框，
             * 免得读成一个可以点的按钮。
             */}
            <span className="text-[11px] text-neutral-500">待审核 {items.length}</span>
            <Button
              disabled={query.isFetching}
              onClick={() => {
                void query.refetch();
              }}
              size="sm"
              type="button"
            >
              刷新
            </Button>
          </div>
        </header>

        {loading ? (
          <div aria-busy="true" aria-label="加载审核队列" className="mt-5 space-y-2">
            <Skeleton className="h-14 w-full" />
            <Skeleton className="h-14 w-full" />
          </div>
        ) : null}

        {query.error ? (
          <p
            className="mt-5 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700"
            role="alert"
          >
            {readableError(query.error)}
          </p>
        ) : null}

        {empty ? (
          <div className="mt-8 rounded-lg border border-dashed border-neutral-300 bg-neutral-50/60 px-6 py-12 text-center">
            <p className="text-sm font-medium text-neutral-700">没有待审核的提交</p>
            <p className="mt-1 text-xs text-neutral-500">
              作者提交草稿之后会出现在这里；刷新一下看看有没有新的。
            </p>
          </div>
        ) : null}

        {items.length > 0 ? (
          <ul className="mt-5 space-y-2">
            {items.map((item) => {
              const selected = item.submission_id === selectedId;
              return (
                <li
                  className={
                    "rounded-lg border bg-white px-4 py-3 transition-colors " +
                    (selected ? "border-neutral-400" : "border-neutral-200")
                  }
                  key={item.submission_id}
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="min-w-0">
                      <h3 className="truncate text-sm font-semibold text-neutral-900">
                        {item.name || "未命名技能"}
                      </h3>
                      <p className="mt-0.5 text-[11px] text-neutral-400">
                        提交于 {formatTimestamp(item.submitted_at)}
                      </p>
                    </div>
                    <Link
                      aria-current={selected ? "true" : undefined}
                      className="shrink-0 text-[11px] font-medium text-neutral-600 underline-offset-2 hover:text-neutral-900 hover:underline"
                      href={detailHref(item.submission_id)}
                      onClick={(event) => {
                        // 单页展开：不要真的跳转（也没有第二个路由可落）
                        event.preventDefault();
                        setSelectedId(selected ? null : item.submission_id);
                      }}
                    >
                      查看详情
                    </Link>
                  </div>
                </li>
              );
            })}
          </ul>
        ) : null}

        {selectedId ? (
          <div className="mt-4">
            <ReviewDetail
              // key：换一条提交时把理由输入框之类的本地状态清掉
              key={selectedId}
              // 缓存失效由详情自己做（它知道审核结果），这里只负责收起详情
              onResolved={() => setSelectedId(null)}
              submissionId={selectedId}
            />
          </div>
        ) : null}

        <p className="mt-8 text-[11px] text-neutral-400">
          审核权限由后端的 admin scope 判定；这里隐藏入口只是为了少一个必然 403 的链接。
        </p>
      </div>
    </div>
  );
}
