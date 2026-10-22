"use client";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  KNOWLEDGE_ACTIVE_STATUSES,
  knowledgeDocumentStatusLabel,
  type KnowledgeDocument,
} from "@/core/api/knowledge";
import { formatBytes, formatTimestamp } from "@/lib/utils";

export interface DocumentListProps {
  documents: readonly KnowledgeDocument[];
  busyId?: string | null;
  onReindex: (document: KnowledgeDocument) => void;
  onDelete: (document: KnowledgeDocument) => void;
}

function statusVariant(status: string): "neutral" | "success" | "danger" | "warning" {
  if (status === "ready") return "success";
  if (status === "failed") return "danger";
  if (KNOWLEDGE_ACTIVE_STATUSES.has(status as KnowledgeDocument["status"])) return "warning";
  return "neutral";
}

export function DocumentList({ documents, busyId = null, onReindex, onDelete }: DocumentListProps) {
  if (!documents.length) {
    return (
      <div className="rounded-lg border border-neutral-200 bg-white px-4 py-10 text-center">
        <p className="text-sm font-medium text-neutral-700">还没有文档</p>
        <p className="mt-1 text-xs text-neutral-500">上传文档后，系统会自动解析、分片并向量化。</p>
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-neutral-200 bg-white">
      <table className="w-full min-w-[660px] border-collapse text-left">
        <thead className="border-b border-neutral-200 bg-neutral-50 text-[11px] text-neutral-500">
          <tr>
            <th className="px-4 py-2.5 font-medium">文件名</th>
            <th className="px-3 py-2.5 font-medium">大小</th>
            <th className="px-3 py-2.5 font-medium">状态</th>
            <th className="px-3 py-2.5 font-medium">分片</th>
            <th className="px-3 py-2.5 font-medium">更新时间</th>
            <th className="px-4 py-2.5 text-right font-medium">操作</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-neutral-100 text-xs text-neutral-700">
          {documents.map((document) => {
            const busy = busyId === document.id;
            const processing = KNOWLEDGE_ACTIVE_STATUSES.has(document.status);
            return (
              <tr className="align-top" key={document.id}>
                <td className="max-w-[260px] px-4 py-3">
                  <span className="block truncate font-medium text-neutral-900" title={document.filename}>
                    {document.filename}
                  </span>
                  {document.status === "failed" && document.error_message ? (
                    <span
                      className="mt-1 block max-w-[260px] truncate text-[11px] text-red-600"
                      title={document.error_message}
                    >
                      {document.error_message}
                    </span>
                  ) : null}
                </td>
                <td className="whitespace-nowrap px-3 py-3">{formatBytes(document.size_bytes)}</td>
                <td className="whitespace-nowrap px-3 py-3">
                  <Badge variant={statusVariant(document.status)}>
                    {knowledgeDocumentStatusLabel(document.status)}
                  </Badge>
                </td>
                <td className="whitespace-nowrap px-3 py-3">
                  {document.status === "ready" ? document.chunk_count : "—"}
                </td>
                <td className="whitespace-nowrap px-3 py-3 text-neutral-500">
                  {formatTimestamp(document.updated_at)}
                </td>
                <td className="whitespace-nowrap px-4 py-3 text-right">
                  <div className="inline-flex gap-1">
                    <Button
                      disabled={busy || processing}
                      onClick={() => onReindex(document)}
                      size="sm"
                      type="button"
                      variant="ghost"
                    >
                      重新索引
                    </Button>
                    <Button
                      disabled={busy}
                      onClick={() => onDelete(document)}
                      size="sm"
                      type="button"
                      variant="danger"
                    >
                      删除
                    </Button>
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
