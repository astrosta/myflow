"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { BookOpenIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import {
  getKnowledgeStatus,
  knowledgeTypeLabel,
  listKnowledgeBases,
  type KnowledgeBase,
} from "@/core/api/knowledge";
import { formatTimestamp } from "@/lib/utils";

import { CreateKnowledgeBaseDialog } from "./create-dialog";

export function KnowledgeListPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);

  const statusQuery = useQuery({
    queryKey: ["knowledge", "status"],
    queryFn: getKnowledgeStatus,
    staleTime: 30_000,
  });
  const basesQuery = useQuery({
    queryKey: ["knowledge", "bases"],
    queryFn: listKnowledgeBases,
    enabled: statusQuery.data?.enabled === true,
    staleTime: 5_000,
  });

  const status = statusQuery.data;
  const error = statusQuery.error ?? basesQuery.error;
  const enabled = status?.enabled === true;

  const openCreated = (base: KnowledgeBase) => {
    setCreating(false);
    void queryClient.invalidateQueries({ queryKey: ["knowledge", "bases"] });
    router.push("/workspace/knowledge/" + encodeURIComponent(base.id));
  };

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto w-full max-w-5xl px-5 py-6 sm:px-8">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-neutral-200 pb-5">
          <div>
            <div className="flex items-center gap-2">
              <BookOpenIcon className="text-neutral-500" size={18} />
              <h1 className="text-xl font-semibold tracking-tight text-neutral-900">知识库</h1>
            </div>
            <p className="mt-1 text-xs leading-relaxed text-neutral-500">
              文档按类型分片、向量化后，对话中的 Agent 可以按类型检索并引用来源。
            </p>
          </div>
          <div className="flex gap-2">
            <Button
              disabled={basesQuery.isFetching}
              onClick={() => {
                void basesQuery.refetch();
                void statusQuery.refetch();
              }}
              size="sm"
              type="button"
            >
              刷新
            </Button>
            <Button
              disabled={!enabled}
              onClick={() => setCreating(true)}
              size="sm"
              type="button"
              variant="primary"
            >
              ＋ 新建知识库
            </Button>
          </div>
        </header>

        {statusQuery.isLoading ? (
          <div className="mt-5 grid gap-3 sm:grid-cols-2" aria-busy="true" aria-label="加载知识库状态">
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-28 w-full" />
          </div>
        ) : null}

        {status && !enabled ? (
          <div className="mt-5 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-relaxed text-amber-900">
            <p className="font-medium">知识库尚未启用</p>
            <p className="mt-1">
              请在 config.yaml 中设置 <code className="font-mono">knowledge_base.enabled: true</code>，
              并填写 MinerU、embedding 与 Milvus 的服务地址后重启网关。
            </p>
          </div>
        ) : null}

        {error ? (
          <p className="mt-5 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700" role="alert">
            {error instanceof Error ? error.message : String(error)}
          </p>
        ) : null}

        {basesQuery.isLoading ? (
          <div className="mt-5 grid gap-3 sm:grid-cols-2" aria-busy="true" aria-label="加载知识库列表">
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-28 w-full" />
          </div>
        ) : null}

        {enabled && !basesQuery.isLoading && !error && (basesQuery.data?.bases.length ?? 0) === 0 ? (
          <div className="mt-8 rounded-lg border border-dashed border-neutral-300 bg-neutral-50/60 px-6 py-12 text-center">
            <BookOpenIcon className="mx-auto text-neutral-300" size={28} />
            <p className="mt-3 text-sm font-medium text-neutral-700">还没有知识库</p>
            <p className="mt-1 text-xs text-neutral-500">
              先创建一个知识库并选择类型，再上传文档。类型决定后续的分片方式。
            </p>
            <Button
              className="mt-4"
              disabled={!enabled}
              onClick={() => setCreating(true)}
              size="sm"
              type="button"
              variant="primary"
            >
              新建知识库
            </Button>
          </div>
        ) : null}

        <div className="mt-5 grid gap-3 sm:grid-cols-2">
          {basesQuery.data?.bases.map((base) => (
            <Link
              className="group rounded-lg border border-neutral-200 bg-white p-4 transition-colors hover:border-neutral-400"
              href={"/workspace/knowledge/" + encodeURIComponent(base.id)}
              key={base.id}
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="truncate text-sm font-semibold text-neutral-900 group-hover:text-neutral-700">
                    {base.name}
                  </h2>
                  <p className="mt-0.5 text-[11px] text-neutral-500">{knowledgeTypeLabel(base.type)}</p>
                </div>
                <span className="shrink-0 text-[11px] text-neutral-400">
                  {formatTimestamp(base.updated_at)}
                </span>
              </div>
              <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-neutral-500">
                <span>{base.document_count} 份文档</span>
                <span>{base.chunk_count} 个分片</span>
              </div>
              {base.description ? (
                <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-neutral-500">{base.description}</p>
              ) : null}
            </Link>
          ))}
        </div>

        <p className="mt-8 text-[11px] text-neutral-400">
          知识库按用户隔离；当前对话只会检索你自己的知识库。
        </p>
      </div>

      {creating ? (
        <CreateKnowledgeBaseDialog onClose={() => setCreating(false)} onCreated={openCreated} />
      ) : null}
    </div>
  );
}
