"use client";

import Link from "next/link";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Markdown } from "@/components/workspace/markdown";
import {
  getKnowledgeBase,
  getKnowledgeDocument,
  getKnowledgeStatus,
  knowledgeDocumentStatusLabel,
  knowledgeTypeLabel,
  KNOWLEDGE_ACTIVE_STATUSES,
  listKnowledgeDocumentChunks,
  type KnowledgeChunk,
  type KnowledgeDocument,
} from "@/core/api/knowledge";
import { formatBytes, formatTimestamp } from "@/lib/utils";

const PAGE_SIZE = 20;

function statusVariant(status: string): "neutral" | "success" | "danger" | "warning" {
  if (status === "ready") return "success";
  if (status === "failed") return "danger";
  if (KNOWLEDGE_ACTIVE_STATUSES.has(status as KnowledgeDocument["status"])) {
    return "warning";
  }
  return "neutral";
}

function MetaItem({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] text-neutral-400">{label}</dt>
      <dd
        className={"mt-0.5 truncate text-xs text-neutral-700" + (mono ? " font-mono" : "")}
        title={value}
      >
        {value}
      </dd>
    </div>
  );
}

function ChunkItem({ chunk }: { chunk: KnowledgeChunk }) {
  return (
    <article
      className="rounded-lg border border-neutral-200 bg-white p-4"
      id={"chunk-" + String(chunk.chunk_index)}
    >
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-neutral-500">
        <span className="font-mono">切片 {chunk.chunk_index + 1}</span>
        {chunk.section ? <span>{chunk.section}</span> : null}
        {chunk.page > 0 ? <span>第 {chunk.page} 页</span> : null}
        {chunk.parent_id ? (
          <span className="rounded bg-neutral-100 px-1.5 py-0.5 font-mono text-[10px] text-neutral-500">
            父块 {chunk.parent_id}
          </span>
        ) : null}
      </div>
      <Markdown className="knowledge-search-md mt-2" text={chunk.content} />
    </article>
  );
}

export interface KnowledgeDocumentPageProps {
  baseId: string;
  documentId: string;
}

/**
 * 单文档详情：元数据来自 SQL 目录，切片正文来自向量库的只读查询。
 *
 * 切片只在 ready 后请求；处理中的文档保持轮询，失败时展示错误而不是
 * 把空切片列表误报成“没有内容”。
 */
export function KnowledgeDocumentPage({ baseId, documentId }: KnowledgeDocumentPageProps) {
  const statusQuery = useQuery({
    queryKey: ["knowledge", "status"],
    queryFn: getKnowledgeStatus,
    staleTime: 30_000,
  });
  const enabled = statusQuery.data?.enabled === true;
  const baseQuery = useQuery({
    queryKey: ["knowledge", "base", baseId],
    queryFn: () => getKnowledgeBase(baseId),
    enabled,
  });
  const documentQuery = useQuery({
    queryKey: ["knowledge", "document", baseId, documentId],
    queryFn: () => getKnowledgeDocument(baseId, documentId),
    enabled,
    refetchInterval: (query) => {
      const current = query.state.data as KnowledgeDocument | undefined;
      return current && KNOWLEDGE_ACTIVE_STATUSES.has(current.status) ? 2_000 : false;
    },
  });
  const document = documentQuery.data;
  const ready = document?.status === "ready";
  const chunksQuery = useInfiniteQuery({
    queryKey: ["knowledge", "document-chunks", baseId, documentId],
    queryFn: ({ pageParam }) =>
      listKnowledgeDocumentChunks(baseId, documentId, pageParam, PAGE_SIZE),
    initialPageParam: 0,
    getNextPageParam: (last) => last.next_offset,
    enabled: enabled && ready,
  });

  const status = statusQuery.data;
  const base = baseQuery.data;
  const loading =
    statusQuery.isLoading ||
    (enabled && (baseQuery.isLoading || documentQuery.isLoading));
  const pageError =
    statusQuery.error ?? (enabled ? baseQuery.error ?? documentQuery.error : null);
  const chunks = chunksQuery.data?.pages.flatMap((page) => page.chunks) ?? [];
  const total = chunksQuery.data?.pages[0]?.count ?? document?.chunk_count ?? 0;

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto w-full max-w-4xl px-5 py-6 sm:px-8">
        <nav className="flex flex-wrap items-center gap-1.5 text-xs text-neutral-500">
          <Link className="hover:text-neutral-900" href="/workspace/knowledge">
            ← 全部知识库
          </Link>
          <span aria-hidden="true">/</span>
          <Link
            className="hover:text-neutral-900"
            href={"/workspace/knowledge/" + encodeURIComponent(baseId)}
          >
            {base?.name ?? "知识库"}
          </Link>
        </nav>

        <header className="mt-3 flex flex-wrap items-center gap-2 border-b border-neutral-200 pb-5">
          <h1 className="min-w-0 truncate text-xl font-semibold tracking-tight text-neutral-900">
            {document?.filename ?? "文档详情"}
          </h1>
          {document ? (
            <Badge variant={statusVariant(document.status)}>
              {knowledgeDocumentStatusLabel(document.status)}
            </Badge>
          ) : null}
        </header>

        {loading ? (
          <div className="mt-5 space-y-3" aria-busy="true" aria-label="加载文档详情">
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-40 w-full" />
          </div>
        ) : null}

        {!enabled && status ? (
          <div className="mt-5 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-relaxed text-amber-900">
            知识库已停用，当前不能查看文档详情。请在 config.yaml 启用后重启网关。
          </div>
        ) : null}

        {pageError ? (
          <p
            className="mt-5 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700"
            role="alert"
          >
            {pageError instanceof Error ? pageError.message : String(pageError)}
          </p>
        ) : null}

        {document && !pageError ? (
          <>
            <section
              aria-labelledby="document-metadata-title"
              className="mt-5 rounded-lg border border-neutral-200 bg-neutral-50/60 p-4"
            >
              <h2 className="text-xs font-medium text-neutral-500" id="document-metadata-title">
                文档信息
              </h2>
              <dl className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-3">
                <MetaItem label="所属知识库" value={base?.name ?? "—"} />
                <MetaItem
                  label="知识库类型"
                  value={base ? knowledgeTypeLabel(base.type) : "—"}
                />
                <MetaItem label="文件类型" value={document.mime_type || "—"} />
                <MetaItem label="文件大小" value={formatBytes(document.size_bytes)} />
                <MetaItem label="分片数量" value={String(document.chunk_count)} />
                <MetaItem label="文档 ID" mono value={document.id} />
                <MetaItem label="创建时间" value={formatTimestamp(document.created_at)} />
                <MetaItem label="更新时间" value={formatTimestamp(document.updated_at)} />
              </dl>
            </section>

            {document.status === "failed" && document.error_message ? (
              <p
                className="mt-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700"
                role="alert"
              >
                处理失败：{document.error_message}
              </p>
            ) : null}

            {KNOWLEDGE_ACTIVE_STATUSES.has(document.status) ? (
              <p
                className="mt-3 rounded-lg border border-sky-200 bg-sky-50 px-4 py-3 text-xs text-sky-800"
                role="status"
              >
                文档正在处理，完成后这里会自动展示切片内容。
              </p>
            ) : null}

            {ready ? (
              <section aria-labelledby="document-chunks-title" className="mt-6">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <h2
                    className="text-xs font-medium text-neutral-500"
                    id="document-chunks-title"
                  >
                    切片内容
                  </h2>
                  <span className="text-[11px] text-neutral-400">共 {total} 个分片</span>
                </div>

                {chunksQuery.isLoading ? (
                  <div className="mt-3 space-y-3" aria-busy="true" aria-label="加载切片">
                    <Skeleton className="h-32 w-full" />
                    <Skeleton className="h-32 w-full" />
                  </div>
                ) : chunksQuery.error ? (
                  <p
                    className="mt-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700"
                    role="alert"
                  >
                    {chunksQuery.error instanceof Error
                      ? chunksQuery.error.message
                      : String(chunksQuery.error)}
                  </p>
                ) : chunks.length === 0 ? (
                  <p className="mt-3 rounded-lg border border-neutral-200 bg-white px-4 py-8 text-center text-xs text-neutral-500">
                    该文档暂无切片内容。
                  </p>
                ) : (
                  <div className="mt-3 space-y-3">
                    {chunks.map((chunk) => (
                      <ChunkItem chunk={chunk} key={chunk.chunk_id} />
                    ))}
                  </div>
                )}

                {chunksQuery.hasNextPage ? (
                  <div className="mt-3 flex justify-center">
                    <Button
                      disabled={chunksQuery.isFetchingNextPage}
                      onClick={() => void chunksQuery.fetchNextPage()}
                      size="sm"
                      type="button"
                    >
                      {chunksQuery.isFetchingNextPage ? "加载中…" : "加载更多"}
                    </Button>
                  </div>
                ) : null}
              </section>
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  );
}
