"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  clearProfileField,
  getProfile,
  MemoryApiError,
  patchProfile,
  type ProfileFieldView,
  type ProfileView,
} from "@/core/api/memory";

const LABELS: Record<string, string> = {
  "identity.preferred_name": "称呼",
  "identity.pronouns": "代词",
  "identity.locale": "地区",
  "identity.timezone": "时区",
  "communication.language": "语言",
  "communication.tone": "语气",
  "communication.verbosity": "详细程度",
  "communication.format": "格式",
  "work.role": "角色",
  "work.domains": "领域",
  "work.tools": "工具",
  "work.expertise": "专长",
  "accessibility.needs": "无障碍需求",
  boundaries: "边界",
};
const LIST_FIELDS = new Set([
  "communication.format",
  "work.domains",
  "work.tools",
  "accessibility.needs",
  "boundaries",
]);

function readableError(error: unknown): string {
  if (error instanceof MemoryApiError) {
    if (error.status === 409) return "画像已被其他操作更新，请重新读取后再改。";
    return error.detail || "请求失败（HTTP " + error.status + "）";
  }
  return error instanceof Error ? error.message : String(error);
}

function formatValue(field: ProfileFieldView): string {
  if (Array.isArray(field.value)) return field.value.join(", ");
  return String(field.value ?? "");
}

export function MyProfilePage() {
  const queryClient = useQueryClient();
  const query = useQuery({ queryKey: ["memory", "profile"], queryFn: getProfile });
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const save = useMutation({
    mutationFn: ({ path, value }: { path: string; value: unknown }) =>
      patchProfile({ [path]: value }, query.data?.revision ?? 0),
    onSuccess: (next: ProfileView) => {
      queryClient.setQueryData(["memory", "profile"], next);
      setEditing(null);
      setError(null);
    },
    onError: (cause) => setError(readableError(cause)),
  });

  const remove = useMutation({
    mutationFn: (path: string) => clearProfileField(path),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memory", "profile"] });
      setError(null);
    },
    onError: (cause) => setError(readableError(cause)),
  });

  if (query.isLoading) {
    return (
      <div className="mx-auto w-full max-w-3xl px-5 py-6 sm:px-8" aria-busy="true">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="mt-4 h-24 w-full" />
      </div>
    );
  }
  if (query.error) {
    return <p className="p-6 text-sm text-red-700" role="alert">{readableError(query.error)}</p>;
  }

  const profile = query.data;
  const entries = Object.entries(profile?.fields ?? {});

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto w-full max-w-3xl px-5 py-6 sm:px-8">
        <header className="border-b border-neutral-200 pb-5">
          <h1 className="text-xl font-semibold tracking-tight text-neutral-900">我的画像</h1>
          <p className="mt-1 text-xs leading-relaxed text-neutral-500">
            这里是你确认过的稳定信息；后续新的高证据对话可以更新它。
          </p>
        </header>
        {error ? (
          <p className="mt-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700" role="alert">
            {error}
          </p>
        ) : null}
        {entries.length === 0 ? (
          <p className="mt-8 rounded-lg border border-dashed border-neutral-300 px-5 py-10 text-center text-sm text-neutral-500">
            还没有画像信息。
          </p>
        ) : (
          <ul className="mt-5 divide-y divide-neutral-100">
            {entries.map(([path, field]) => (
              <li className="py-4" key={path}>
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-xs font-medium text-neutral-500">{LABELS[path] ?? path}</p>
                    {editing === path ? (
                      <input
                        aria-label={path}
                        className="mt-2 w-full rounded-md border border-neutral-300 px-3 py-2 text-sm"
                        onChange={(event) => setDraft(event.target.value)}
                        value={draft}
                      />
                    ) : (
                      <p className="mt-1 text-sm text-neutral-900">{formatValue(field)}</p>
                    )}
                    <p className="mt-1 text-[11px] text-neutral-400">
                      {field.source} · {field.evidence_tier ?? "UNKNOWN"} · {Math.round(field.confidence * 100)}%
                    </p>
                  </div>
                  <div className="flex shrink-0 gap-2">
                    {editing === path ? (
                      <>
                        <Button
                          onClick={() => save.mutate({ path, value: normalize(path, draft) })}
                          size="sm"
                          type="button"
                          variant="primary"
                        >
                          保存
                        </Button>
                        <Button onClick={() => setEditing(null)} size="sm" type="button">
                          取消
                        </Button>
                      </>
                    ) : (
                      <Button
                        onClick={() => {
                          setEditing(path);
                          setDraft(formatValue(field));
                          setError(null);
                        }}
                        size="sm"
                        type="button"
                      >
                        编辑
                      </Button>
                    )}
                    <Button
                      disabled={remove.isPending}
                      onClick={() => remove.mutate(path)}
                      size="sm"
                      type="button"
                      variant="danger"
                    >
                      删除
                    </Button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function normalize(path: string, value: string): unknown {
  if (LIST_FIELDS.has(path)) {
    return value.split(",").map((item) => item.trim()).filter(Boolean);
  }
  return value.trim();
}
