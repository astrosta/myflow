"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  acceptProfileSuggestion,
  clearProfileField,
  dismissProfileSuggestion,
  getOnboarding,
  getProfile,
  listProfileSuggestions,
  MemoryApiError,
  patchProfile,
  updateOnboarding,
  type OnboardingAction,
  type ProfileFieldView,
  type ProfileSuggestion,
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
  const openviking = Boolean(query.data?.etag);
  const onboarding = useQuery({
    queryKey: ["memory", "onboarding"],
    queryFn: getOnboarding,
    enabled: openviking,
  });
  const suggestions = useQuery({
    queryKey: ["memory", "suggestions"],
    queryFn: listProfileSuggestions,
    enabled: openviking,
  });
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
      void queryClient.invalidateQueries({ queryKey: ["memory", "onboarding"] });
      void queryClient.invalidateQueries({ queryKey: ["memory", "suggestions"] });
    },
    onError: (cause) => setError(readableError(cause)),
  });

  const remove = useMutation({
    mutationFn: (path: string) => clearProfileField(path),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memory", "profile"] });
      void queryClient.invalidateQueries({ queryKey: ["memory", "suggestions"] });
      setError(null);
    },
    onError: (cause) => setError(readableError(cause)),
  });

  const onboardingAction = useMutation({
    mutationFn: (action: OnboardingAction) => updateOnboarding(action),
    onSuccess: (next) => {
      queryClient.setQueryData(["memory", "onboarding"], next);
      setError(null);
    },
    onError: (cause) => setError(readableError(cause)),
  });

  const acceptSuggestion = useMutation({
    mutationFn: (suggestion: ProfileSuggestion) =>
      acceptProfileSuggestion(suggestion.id, query.data?.etag),
    onSuccess: (result) => {
      queryClient.setQueryData(["memory", "profile"], result.profile);
      void queryClient.invalidateQueries({ queryKey: ["memory", "suggestions"] });
      void queryClient.invalidateQueries({ queryKey: ["memory", "onboarding"] });
      setError(null);
    },
    onError: (cause) => setError(readableError(cause)),
  });

  const dismissSuggestion = useMutation({
    mutationFn: (suggestion: ProfileSuggestion) =>
      dismissProfileSuggestion(suggestion.id, query.data?.etag),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memory", "suggestions"] });
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
  const suggestionItems = suggestions.data?.items ?? [];

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
        {onboarding.data?.should_show ? (
          <section className="mt-4 rounded-xl border border-sky-200 bg-sky-50/50 p-4" aria-label="完善画像">
            <h2 className="text-sm font-semibold text-neutral-900">让助手更懂你</h2>
            <p className="mt-1 text-xs leading-relaxed text-neutral-600">
              补充称呼、语言、沟通偏好或工作角色，之后对话会更贴合你的习惯。不填也不影响使用。
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
              <Button
                onClick={() => document.getElementById("profile-fields")?.scrollIntoView({ behavior: "smooth" })}
                size="sm"
                type="button"
                variant="primary"
              >
                去填写
              </Button>
              <Button
                disabled={onboardingAction.isPending}
                onClick={() => onboardingAction.mutate("snooze")}
                size="sm"
                type="button"
              >
                稍后
              </Button>
              <Button
                disabled={onboardingAction.isPending}
                onClick={() => onboardingAction.mutate("dismiss")}
                size="sm"
                type="button"
              >
                不再提醒
              </Button>
            </div>
          </section>
        ) : null}
        {suggestionItems.length > 0 ? (
          <section className="mt-5" aria-label="画像建议">
            <div className="flex items-baseline justify-between gap-3">
              <h2 className="text-xs font-medium text-neutral-500">发现的画像信息</h2>
              <p className="text-[11px] text-neutral-400">确认后才会写入画像</p>
            </div>
            <ul className="mt-3 space-y-2">
              {suggestionItems.map((suggestion) => (
                <li className="rounded-lg border border-amber-200 bg-amber-50/40 p-4" key={suggestion.id}>
                  <p className="text-xs font-medium text-neutral-500">
                    {LABELS[suggestion.field_path] ?? suggestion.field_path}
                  </p>
                  <p className="mt-1 text-sm text-neutral-900">{formatSuggestionValue(suggestion.value)}</p>
                  <p className="mt-1 text-[11px] leading-relaxed text-neutral-500">
                    {suggestion.reason} · 置信度 {Math.round(suggestion.confidence * 100)}%
                  </p>
                  {suggestion.evidence_quote ? (
                    <blockquote className="mt-2 line-clamp-2 border-l-2 border-amber-200 pl-3 text-xs text-neutral-500">
                      {suggestion.evidence_quote}
                    </blockquote>
                  ) : null}
                  <div className="mt-3 flex gap-2">
                    <Button
                      disabled={acceptSuggestion.isPending || dismissSuggestion.isPending}
                      onClick={() => acceptSuggestion.mutate(suggestion)}
                      size="sm"
                      type="button"
                      variant="primary"
                    >
                      接受
                    </Button>
                    <Button
                      disabled={acceptSuggestion.isPending || dismissSuggestion.isPending}
                      onClick={() => dismissSuggestion.mutate(suggestion)}
                      size="sm"
                      type="button"
                    >
                      忽略
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
        {entries.length === 0 ? (
          <p className="mt-8 rounded-lg border border-dashed border-neutral-300 px-5 py-10 text-center text-sm text-neutral-500">
            还没有画像信息。
          </p>
        ) : (
          <ul className="mt-5 divide-y divide-neutral-100" id="profile-fields">
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

function formatSuggestionValue(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  return String(value ?? "");
}

function normalize(path: string, value: string): unknown {
  if (LIST_FIELDS.has(path)) {
    return value.split(",").map((item) => item.trim()).filter(Boolean);
  }
  return value.trim();
}
