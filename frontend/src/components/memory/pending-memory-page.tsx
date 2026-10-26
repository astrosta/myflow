"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  deleteMemoryItem,
  listPending,
  MemoryApiError,
  promoteSemantic,
  type PendingItem,
} from "@/core/api/memory";

function readableError(error: unknown): string {
  if (error instanceof MemoryApiError) {
    if (error.status === 409) return "这条记忆已被其他操作更新，请刷新后再试。";
    return error.detail || "请求失败（HTTP " + error.status + "）";
  }
  return error instanceof Error ? error.message : String(error);
}

export function PendingMemoryPage() {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["memory", "pending"], queryFn: listPending });
  const [error, setError] = useState<string | null>(null);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["memory", "pending"] });
  };
  const promote = useMutation({
    mutationFn: (item: PendingItem) => promoteSemantic(item.id, item.revision),
    onSuccess: () => {
      setError(null);
      refresh();
    },
    onError: (cause) => setError(readableError(cause)),
  });
  const remove = useMutation({
    mutationFn: (item: PendingItem) => deleteMemoryItem("semantic", item.id),
    onSuccess: () => {
      setError(null);
      refresh();
    },
    onError: (cause) => setError(readableError(cause)),
  });

  if (query.isLoading) {
    return (
      <div className="mx-auto w-full max-w-3xl px-5 py-6 sm:px-8" aria-busy="true">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="mt-4 h-24 w-full" />
      </div>
    );
  }
  if (query.error) {
    return <p className="p-6 text-sm text-red-700" role="alert">{readableError(query.error)}</p>;
  }

  const items = query.data?.items ?? [];
  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto w-full max-w-3xl px-5 py-6 sm:px-8">
        <header className="border-b border-neutral-200 pb-5">
          <h1 className="text-xl font-semibold tracking-tight text-neutral-900">待确认记忆</h1>
          <p className="mt-1 text-xs leading-relaxed text-neutral-500">
            这些是从对话中观察到、但证据还不够强的语义记忆。确认后才会进入检索。
          </p>
        </header>
        {error ? (
          <p className="mt-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700" role="alert">
            {error}
          </p>
        ) : null}
        {items.length === 0 ? (
          <p className="mt-8 rounded-lg border border-dashed border-neutral-300 px-5 py-10 text-center text-sm text-neutral-500">
            暂无待确认记忆。
          </p>
        ) : (
          <ul className="mt-5 space-y-3">
            {items.map((item) => (
              <li className="rounded-lg border border-neutral-200 bg-white p-4" key={item.id}>
                <p className="text-sm font-medium text-neutral-900">{item.content}</p>
                <p className="mt-2 text-[11px] text-neutral-500">
                  置信度 {Math.round(item.confidence * 100)}% · {item.evidence_tier ?? "UNKNOWN"} · {item.source}
                </p>
                {item.evidence_quote ? (
                  <blockquote className="mt-2 border-l-2 border-neutral-200 pl-3 text-xs leading-relaxed text-neutral-500">
                    {item.evidence_quote}
                  </blockquote>
                ) : null}
                <div className="mt-3 flex gap-2">
                  <Button
                    disabled={promote.isPending}
                    onClick={() => promote.mutate(item)}
                    size="sm"
                    type="button"
                    variant="primary"
                  >
                    确认保留
                  </Button>
                  <Button
                    disabled={remove.isPending}
                    onClick={() => remove.mutate(item)}
                    size="sm"
                    type="button"
                    variant="danger"
                  >
                    删除
                  </Button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
