"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  deleteKnowledgeBase,
  deleteKnowledgeDocument,
  getKnowledgeBase,
  getKnowledgeStatus,
  KNOWLEDGE_ACTIVE_STATUSES,
  knowledgeTypeLabel,
  listKnowledgeDocuments,
  reindexKnowledgeDocument,
  searchKnowledge,
  uploadKnowledgeFiles,
  type KnowledgeDocument,
  type KnowledgeSearchResult,
} from "@/core/api/knowledge";
import { formatTimestamp } from "@/lib/utils";

import { DocumentList } from "./document-list";
import { KnowledgeSearch } from "./knowledge-search";
import { UploadDropzone } from "./upload-dropzone";

export function KnowledgeDetailPage({ baseId }: { baseId: string }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [uploading, setUploading] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const statusQuery = useQuery({
    queryKey: ["knowledge", "status"],
    queryFn: getKnowledgeStatus,
    staleTime: 30_000,
  });
  const docsQuery = useQuery({
    queryKey: ["knowledge", "documents", baseId],
    queryFn: () => listKnowledgeDocuments(baseId),
    enabled: statusQuery.data?.enabled === true,
    refetchInterval: (query) => {
      const data = query.state.data as { documents?: KnowledgeDocument[] } | undefined;
      return data?.documents?.some((item) => KNOWLEDGE_ACTIVE_STATUSES.has(item.status)) ? 2_000 : false;
    },
  });
  const baseQuery = useQuery({
    queryKey: ["knowledge", "base", baseId],
    queryFn: () => getKnowledgeBase(baseId),
    enabled: statusQuery.data?.enabled === true,
    refetchInterval: () => {
      const active = docsQuery.data?.documents.some((item) => KNOWLEDGE_ACTIVE_STATUSES.has(item.status));
      return active ? 2_000 : false;
    },
  });

  const status = statusQuery.data;
  const base = baseQuery.data;
  const docs = docsQuery.data?.documents ?? [];
  const error = baseQuery.error ?? docsQuery.error;
  const enabled = status?.enabled === true;

  const refresh = () => {
    void docsQuery.refetch();
    void baseQuery.refetch();
  };

  const upload = async (files: File[]) => {
    if (!files.length || uploading) return;
    setUploading(true);
    setActionError(null);
    try {
      await uploadKnowledgeFiles(baseId, files);
      await queryClient.invalidateQueries({ queryKey: ["knowledge", "documents", baseId] });
      await queryClient.invalidateQueries({ queryKey: ["knowledge", "base", baseId] });
      await queryClient.invalidateQueries({ queryKey: ["knowledge", "bases"] });
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setUploading(false);
    }
  };

  const removeDocument = async (document: KnowledgeDocument) => {
    if (!window.confirm("删除文档《" + document.filename + "》及其全部分片？")) return;
    setBusyId(document.id);
    setActionError(null);
    try {
      await deleteKnowledgeDocument(baseId, document.id);
      await queryClient.invalidateQueries({ queryKey: ["knowledge", "documents", baseId] });
      await queryClient.invalidateQueries({ queryKey: ["knowledge", "base", baseId] });
      await queryClient.invalidateQueries({ queryKey: ["knowledge", "bases"] });
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusyId(null);
    }
  };

  const reindex = async (document: KnowledgeDocument) => {
    setBusyId(document.id);
    setActionError(null);
    try {
      await reindexKnowledgeDocument(baseId, document.id);
      await queryClient.invalidateQueries({ queryKey: ["knowledge", "documents", baseId] });
      await queryClient.invalidateQueries({ queryKey: ["knowledge", "base", baseId] });
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusyId(null);
    }
  };

  const removeBase = async () => {
    if (!base || !window.confirm("删除知识库《" + base.name + "》及其全部文档和分片？此操作不可恢复。")) return;
    setActionError(null);
    try {
      await deleteKnowledgeBase(baseId);
      await queryClient.invalidateQueries({ queryKey: ["knowledge", "bases"] });
      router.push("/workspace/knowledge");
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  const search = (query: string, keyword: string): Promise<KnowledgeSearchResult> =>
    searchKnowledge({ query, keyword, baseId, topK: 5 });

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto w-full max-w-5xl px-5 py-6 sm:px-8">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-neutral-200 pb-5">
          <div className="min-w-0">
            <Link className="text-xs text-neutral-500 hover:text-neutral-900" href="/workspace/knowledge">
              ← 全部知识库
            </Link>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <h1 className="truncate text-xl font-semibold tracking-tight text-neutral-900">
                {base?.name ?? "知识库"}
              </h1>
              {base ? <Badge variant="outline">{knowledgeTypeLabel(base.type)}</Badge> : null}
            </div>
            <p className="mt-1 text-xs text-neutral-500">
              {base
                ? String(base.document_count) + " 份文档 · " + String(base.chunk_count) + " 个分片"
                : "加载中…"}
              {base ? " · 更新于 " + formatTimestamp(base.updated_at) : ""}
            </p>
          </div>
          <div className="flex gap-2">
            <Button disabled={docsQuery.isFetching} onClick={refresh} size="sm" type="button">
              刷新
            </Button>
            <Button disabled={!base} onClick={() => void removeBase()} size="sm" type="button" variant="danger">
              删除知识库
            </Button>
          </div>
        </div>

        {!enabled && status ? (
          <div className="mt-5 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-relaxed text-amber-900">
            知识库已停用，当前不能上传或检索。请在 config.yaml 启用后重启网关。
          </div>
        ) : null}

        {error ? (
          <p className="mt-5 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700" role="alert">
            {error instanceof Error ? error.message : String(error)}
          </p>
        ) : null}

        {actionError ? (
          <p className="mt-5 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700" role="alert">
            {actionError}
          </p>
        ) : null}

        <section className="mt-5">
          <UploadDropzone
            allowedExtensions={status?.allowed_extensions ?? ["pdf", "docx", "md", "txt"]}
            disabled={!enabled || !base}
            maxFileSizeBytes={status?.max_file_size_bytes ?? 50 * 1024 * 1024}
            maxFiles={status?.max_files_per_request ?? 10}
            onUpload={(files) => void upload(files)}
            uploading={uploading}
          />
        </section>

        <section aria-labelledby="documents-title" className="mt-7">
          <div className="mb-2 flex items-center justify-between">
            <div>
              <h2 className="text-sm font-semibold text-neutral-900" id="documents-title">
                文档
              </h2>
              <p className="mt-0.5 text-[11px] text-neutral-500">
                后台会依次解析、清洗、分片、向量化；处理中会自动刷新状态。
              </p>
            </div>
            {docsQuery.isFetching ? <span className="text-[11px] text-neutral-400">同步中…</span> : null}
          </div>
          {docsQuery.isLoading ? (
            <div className="space-y-2" aria-busy="true" aria-label="加载文档">
              <Skeleton className="h-12 w-full" />
              <Skeleton className="h-12 w-full" />
            </div>
          ) : (
            <DocumentList
              busyId={busyId}
              documents={docs}
              onDelete={(document) => void removeDocument(document)}
              onReindex={(document) => void reindex(document)}
            />
          )}
        </section>

        <section className="mt-8 border-t border-neutral-200 pt-6">
          <KnowledgeSearch onSearch={search} />
        </section>
      </div>
    </div>
  );
}
