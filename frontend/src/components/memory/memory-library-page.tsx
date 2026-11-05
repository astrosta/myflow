"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Markdown } from "@/components/workspace/markdown";
import {
  deleteMemoryItem,
  getMemoryContent,
  listMemoryItems,
  listMemoryTypes,
  saveMemoryContent,
} from "@/core/api/memory-items";
import { MemoryApiError } from "@/core/api/memory";

function readableError(error: unknown): string {
  if (error instanceof MemoryApiError) {
    if (error.status === 503) return "长期记忆未启用或暂不可用，请稍后重试。";
    if (error.status === 409) return "这条记忆已被更新，请重新打开后再改。";
    if (error.status === 404) return "记忆已不存在，请刷新列表。";
    return error.detail || "请求失败（HTTP " + error.status + "）";
  }
  return error instanceof Error ? error.message : String(error);
}

function displayName(name: string): string {
  return name.replace(/\.md$/i, "").replace(/[_-]+/g, " ").trim() || name;
}

export function MemoryLibraryPage() {
  const queryClient = useQueryClient();
  const typesQuery = useQuery({
    queryKey: ["memory", "types"],
    queryFn: listMemoryTypes,
    staleTime: Number.POSITIVE_INFINITY,
  });
  const [activeType, setActiveType] = useState("preferences");
  const [selectedUri, setSelectedUri] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const labels = new Map(
    (typesQuery.data?.items ?? []).map((item) => [item.type, item.label]),
  );
  const listQuery = useQuery({
    queryKey: ["memory", "items", activeType],
    queryFn: () => listMemoryItems(activeType),
  });
  const contentQuery = useQuery({
    queryKey: ["memory", "item-content", selectedUri],
    queryFn: () => getMemoryContent(selectedUri as string),
    enabled: Boolean(selectedUri),
  });

  const save = useMutation({
    mutationFn: () =>
      saveMemoryContent(selectedUri as string, draft, contentQuery.data?.sha256 ?? ""),
    onSuccess: (next) => {
      queryClient.setQueryData(["memory", "item-content", selectedUri], next);
      setEditing(false);
      setError(null);
      setNotice("已保存；摘要会在 OpenViking 重新生成后刷新。");
      void queryClient.invalidateQueries({ queryKey: ["memory", "items"] });
    },
    onError: (cause) => setError(readableError(cause)),
  });

  const remove = useMutation({
    mutationFn: () => deleteMemoryItem(selectedUri as string),
    onSuccess: () => {
      setSelectedUri(null);
      setEditing(false);
      setError(null);
      setNotice("记忆已删除。");
      void queryClient.invalidateQueries({ queryKey: ["memory", "items"] });
    },
    onError: (cause) => setError(readableError(cause)),
  });

  const selectItem = (uri: string) => {
    setSelectedUri(uri);
    setEditing(false);
    setDraft("");
    setError(null);
    setNotice(null);
  };

  if (typesQuery.isLoading) {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-6 sm:px-8" aria-busy="true">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="mt-4 h-10 w-full" />
        <Skeleton className="mt-4 h-64 w-full" />
      </div>
    );
  }
  if (typesQuery.error) {
    return (
      <div className="mx-auto w-full max-w-3xl px-5 py-10 sm:px-8">
        <h1 className="text-xl font-semibold tracking-tight text-neutral-900">记忆管理</h1>
        <p
          className="mt-4 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-xs leading-relaxed text-amber-900"
          role="alert"
        >
          {readableError(typesQuery.error)}
        </p>
      </div>
    );
  }

  const types = typesQuery.data?.items ?? [];
  const items = listQuery.data?.items ?? [];
  const content = contentQuery.data;
  const selectedItem = selectedUri
    ? (items.find((item) => item.uri === selectedUri) ?? null)
    : null;
  const selectedTitle = displayName(
    selectedItem?.name ?? selectedUri?.split("/").pop() ?? "",
  );

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto w-full max-w-5xl px-5 py-6 sm:px-8">
        <header className="border-b border-neutral-200 pb-5">
          <h1 className="text-xl font-semibold tracking-tight text-neutral-900">记忆管理</h1>
          <p className="mt-1 text-xs leading-relaxed text-neutral-500">
            按类型查看助手自动抽取的长期记忆；左侧目录选择一条记忆，右侧查看、编辑或删除内容。
          </p>
        </header>

        {error ? (
          <p
            className="mt-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700"
            role="alert"
          >
            {error}
          </p>
        ) : null}
        {notice ? (
          <p className="mt-4 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-xs text-emerald-800" role="status">
            {notice}
          </p>
        ) : null}

        <nav aria-label="记忆类型" className="mt-4 flex flex-wrap gap-2">
          {types.map((item) => (
            <Button
              aria-pressed={activeType === item.type}
              key={item.type}
              onClick={() => {
                setActiveType(item.type);
                setSelectedUri(null);
                setEditing(false);
                setError(null);
                setNotice(null);
              }}
              size="sm"
              type="button"
              variant={activeType === item.type ? "primary" : "secondary"}
            >
              {item.label}
            </Button>
          ))}
        </nav>

        <div className="mt-5 grid items-start gap-5 lg:grid-cols-[320px_minmax(0,1fr)]">
          <section aria-label="记忆目录" className="min-w-0">
            <div className="overflow-hidden rounded-lg border border-neutral-200 bg-white">
              <div className="border-b border-neutral-100 px-4 py-3">
                <h2 className="text-sm font-semibold text-neutral-900">记忆目录</h2>
                <p className="mt-0.5 text-[11px] text-neutral-400">
                  {labels.get(activeType) ?? activeType}
                </p>
              </div>

              {listQuery.isLoading ? (
                <div className="space-y-2 p-3" aria-busy="true">
                  <Skeleton className="h-16 w-full" />
                  <Skeleton className="h-16 w-full" />
                </div>
              ) : listQuery.error ? (
                <p className="m-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700" role="alert">
                  {readableError(listQuery.error)}
                </p>
              ) : items.length === 0 ? (
                <p className="px-4 py-10 text-center text-xs text-neutral-500">
                  这个类型还没有记忆。
                </p>
              ) : (
                <ul className="divide-y divide-neutral-100">
                  {items.map((item) => {
                    const selected = selectedUri === item.uri;
                    return (
                      <li key={item.uri}>
                        <button
                          aria-current={selected ? "true" : undefined}
                          className={
                            "w-full px-4 py-3 text-left transition-colors " +
                            (selected
                              ? "bg-neutral-900 text-white"
                              : "text-neutral-900 hover:bg-neutral-50")
                          }
                          onClick={() => selectItem(item.uri)}
                          type="button"
                        >
                          <span className="block truncate text-sm font-medium">
                            {displayName(item.name)}
                          </span>
                          <span
                            className={
                              "mt-1 block line-clamp-2 text-xs leading-relaxed " +
                              (selected ? "text-neutral-300" : "text-neutral-600")
                            }
                          >
                            {item.abstract || "暂无摘要"}
                          </span>
                          {item.updated_at ? (
                            <span
                              className="mt-1 block text-[11px] text-neutral-400"
                            >
                              {item.updated_at}
                            </span>
                          ) : null}
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
              {listQuery.data?.has_more ? (
                <p className="border-t border-neutral-100 px-4 py-2 text-[11px] text-neutral-400">
                  仅显示前 {listQuery.data.limit} 条记忆。
                </p>
              ) : null}
            </div>
          </section>

          <section aria-label="记忆内容" className="min-w-0 rounded-lg border border-neutral-200 p-4">
            {!selectedUri ? (
              <p className="py-10 text-center text-xs text-neutral-500">
                从左侧记忆目录选择一条记忆后，这里会显示它的内容。
              </p>
            ) : (
              <>
                <div className="flex flex-wrap items-center justify-between gap-2 border-b border-neutral-100 pb-3">
                  <h2 className="min-w-0 truncate text-sm font-semibold text-neutral-900">
                    {selectedTitle}
                  </h2>
                  <div className="flex gap-2">
                    <Button
                      disabled={!content || editing || save.isPending}
                      onClick={() => {
                        setDraft(content?.content ?? "");
                        setEditing(true);
                        setError(null);
                        setNotice(null);
                      }}
                      size="sm"
                      type="button"
                    >
                      编辑
                    </Button>
                    <Button
                      disabled={!selectedUri || remove.isPending || save.isPending}
                      onClick={() => {
                        if (!window.confirm("确定删除这条记忆？删除后无法恢复。")) return;
                        remove.mutate();
                      }}
                      size="sm"
                      type="button"
                      variant="danger"
                    >
                      删除
                    </Button>
                  </div>
                </div>

                <div className="mt-4">
                  {contentQuery.isLoading ? (
                    <Skeleton className="h-48 w-full" />
                  ) : contentQuery.error ? (
                    <p className="rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700" role="alert">
                      {readableError(contentQuery.error)}
                    </p>
                  ) : content ? (
                    editing ? (
                      <form
                        onSubmit={(event) => {
                          event.preventDefault();
                          save.mutate();
                        }}
                      >
                        <textarea
                          aria-label="记忆正文"
                          className="block h-64 w-full rounded-md border border-neutral-300 px-3 py-2 font-mono text-xs leading-relaxed text-neutral-900"
                          onChange={(event) => setDraft(event.target.value)}
                          value={draft}
                        />
                        <div className="mt-2 flex gap-2">
                          <Button disabled={save.isPending} size="sm" type="submit" variant="primary">
                            {save.isPending ? "保存中…" : "保存修改"}
                          </Button>
                          <Button
                            disabled={save.isPending}
                            onClick={() => {
                              setEditing(false);
                              setDraft("");
                            }}
                            size="sm"
                            type="button"
                          >
                            取消
                          </Button>
                        </div>
                      </form>
                    ) : (
                      <div className="rounded-lg border border-neutral-100 bg-neutral-50/40 p-3">
                        <Markdown className="knowledge-search-md" text={content.content} />
                      </div>
                    )
                  ) : null}
                </div>
              </>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
