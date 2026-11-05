"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  acceptProfileSuggestion,
  clearProfile,
  dismissProfileSuggestion,
  getOnboarding,
  getProfile,
  getProfileSchema,
  listProfileSuggestions,
  MemoryApiError,
  patchProfile,
  updateOnboarding,
  type OnboardingAction,
  type ProfileFieldSpec,
  type ProfileFieldView,
  type ProfileSuggestion,
  type ProfileView,
} from "@/core/api/memory";

const GROUP_ORDER = ["identity", "communication", "work", "accessibility", "boundaries"];

function readableError(error: unknown): string {
  if (error instanceof MemoryApiError) {
    if (error.status === 409) return "画像已被其他操作更新，请刷新后再改。";
    return error.detail || "请求失败（HTTP " + error.status + "）";
  }
  return error instanceof Error ? error.message : String(error);
}

function formatProfileValue(
  path: string,
  value: unknown,
  specs: Record<string, ProfileFieldSpec>,
): string {
  const spec = specs[path];
  if (Array.isArray(value)) {
    const labels = new Map((spec?.options ?? []).map((option) => [option.value, option.label]));
    return value.map((item) => labels.get(String(item)) ?? String(item)).join("、");
  }
  if (typeof value === "string" && spec?.options.length) {
    return spec.options.find((option) => option.value === value)?.label ?? value;
  }
  return String(value ?? "");
}

function draftValueFromField(spec: ProfileFieldSpec, value: unknown): string {
  if (Array.isArray(value)) return value.join("、");
  if (value === null || value === undefined) return "";
  return String(value);
}

export function draftFromProfile(
  specs: readonly ProfileFieldSpec[],
  fields: Record<string, ProfileFieldView>,
): Record<string, string> {
  const draft: Record<string, string> = {};
  for (const spec of specs) {
    draft[spec.path] = draftValueFromField(spec, fields[spec.path]?.value);
  }
  return draft;
}

function splitItems(raw: string): string[] {
  return raw
    .split(/[、,，]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function serializeValue(spec: ProfileFieldSpec, raw: string): unknown {
  if (spec.kind === "string_list") {
    const items = splitItems(raw);
    return items.length ? items : null;
  }
  if (spec.kind === "multi_choice") {
    const picked = new Set(splitItems(raw));
    const ordered = spec.options
      .map((option) => option.value)
      .filter((value) => picked.has(value));
    return ordered.length ? ordered : null;
  }
  const text = raw.trim();
  return text ? text : null;
}

/**
 * 只提交与当前画像不同的字段；清空的字段提交 null，由后端删除该字段。
 */
export function changedFields(
  specs: readonly ProfileFieldSpec[],
  draft: Record<string, string>,
  fields: Record<string, ProfileFieldView>,
): Record<string, unknown> {
  const changes: Record<string, unknown> = {};
  for (const spec of specs) {
    const next = serializeValue(spec, draft[spec.path] ?? "");
    const before = serializeValue(
      spec,
      draftValueFromField(spec, fields[spec.path]?.value),
    );
    if (JSON.stringify(next) !== JSON.stringify(before)) {
      changes[spec.path] = next;
    }
  }
  return changes;
}

function groupedFields(specs: readonly ProfileFieldSpec[]) {
  const groups: { key: string; label: string; fields: ProfileFieldSpec[] }[] = [];
  for (const spec of specs) {
    const existing = groups.find((group) => group.key === spec.group);
    if (existing) {
      existing.fields.push(spec);
      continue;
    }
    groups.push({ key: spec.group, label: spec.group_label, fields: [spec] });
  }
  return groups.sort((a, b) => GROUP_ORDER.indexOf(a.key) - GROUP_ORDER.indexOf(b.key));
}

export function MyProfilePage() {
  const queryClient = useQueryClient();
  const profileQuery = useQuery({ queryKey: ["memory", "profile"], queryFn: getProfile });
  const schemaQuery = useQuery({
    queryKey: ["memory", "profile-schema"],
    queryFn: getProfileSchema,
    staleTime: Number.POSITIVE_INFINITY,
  });
  const openviking = Boolean(profileQuery.data?.etag);
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

  // 草稿按画像版本（etag/revision）打戳：保存成功后 token 变化，草稿自动回落到
  // 服务端返回的新值，不需要在 effect 里 setState（也避免覆盖用户的并发输入）。
  const [form, setForm] = useState<{ token: string; draft: Record<string, string> } | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  const specs = useMemo(() => schemaQuery.data?.fields ?? [], [schemaQuery.data]);
  const specByPath = useMemo(
    () => Object.fromEntries(specs.map((spec) => [spec.path, spec])),
    [specs],
  );
  const profile = profileQuery.data;
  const profileToken = profile ? profile.etag ?? String(profile.revision) : "";
  const draft = useMemo(
    () =>
      form && form.token === profileToken
        ? form.draft
        : profile
          ? draftFromProfile(specs, profile.fields)
          : {},
    [form, profileToken, profile, specs],
  );
  const updateField = (path: string, value: string) =>
    setForm({ token: profileToken, draft: { ...draft, [path]: value } });

  const saveAll = useMutation({
    mutationFn: (fields: Record<string, unknown>) =>
      patchProfile(fields, profileQuery.data?.revision ?? 0),
    onSuccess: (next: ProfileView) => {
      queryClient.setQueryData(["memory", "profile"], next);
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["memory", "onboarding"] });
      void queryClient.invalidateQueries({ queryKey: ["memory", "suggestions"] });
    },
    onError: (cause) => setError(readableError(cause)),
  });

  const clearAll = useMutation({
    mutationFn: () => clearProfile(profileQuery.data?.etag),
    onSuccess: (next: ProfileView) => {
      queryClient.setQueryData(["memory", "profile"], next);
      setError(null);
      void queryClient.invalidateQueries({ queryKey: ["memory", "suggestions"] });
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
      acceptProfileSuggestion(suggestion.id, profileQuery.data?.etag),
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
      dismissProfileSuggestion(suggestion.id, profileQuery.data?.etag),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["memory", "suggestions"] });
      setError(null);
    },
    onError: (cause) => setError(readableError(cause)),
  });

  const loading = profileQuery.isLoading || schemaQuery.isLoading;
  const loadError = profileQuery.error ?? schemaQuery.error;
  if (loading) {
    return (
      <div className="mx-auto w-full max-w-3xl px-5 py-6 sm:px-8" aria-busy="true">
        <Skeleton className="h-8 w-32" />
        <Skeleton className="mt-4 h-24 w-full" />
        <Skeleton className="mt-3 h-40 w-full" />
      </div>
    );
  }
  if (loadError) {
    return (
      <p className="p-6 text-sm text-red-700" role="alert">
        {readableError(loadError)}
      </p>
    );
  }

  const changes = changedFields(specs, draft, profile?.fields ?? {});
  const dirty = Object.keys(changes).length > 0;
  const suggestionItems = suggestions.data?.items ?? [];

  const resetDraft = () => {
    setForm(null);
    setError(null);
  };

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto w-full max-w-3xl px-5 py-6 sm:px-8">
        <header className="border-b border-neutral-200 pb-5">
          <h1 className="text-xl font-semibold tracking-tight text-neutral-900">我的画像</h1>
          <p className="mt-1 text-xs leading-relaxed text-neutral-500">
            全部字段都在这里：没有填写的就是空白，填好后点“保存修改”。你也可以在对话里明确告诉我，
            我再更新这些字段。
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

        {onboarding.data?.should_show ? (
          <section
            className="mt-4 rounded-xl border border-sky-200 bg-sky-50/50 p-4"
            aria-label="完善画像"
          >
            <h2 className="text-sm font-semibold text-neutral-900">让助手更懂你</h2>
            <p className="mt-1 text-xs leading-relaxed text-neutral-600">
              下面的字段都可以填：称呼、语言、沟通偏好、工作信息。不填也不影响使用。
            </p>
            <div className="mt-3 flex flex-wrap gap-2">
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
                <li
                  className="rounded-lg border border-amber-200 bg-amber-50/40 p-4"
                  key={suggestion.id}
                >
                  <p className="text-xs font-medium text-neutral-500">
                    {specByPath[suggestion.field_path]?.label ?? suggestion.field_path}
                  </p>
                  <p className="mt-1 text-sm text-neutral-900">
                    {formatProfileValue(suggestion.field_path, suggestion.value, specByPath)}
                  </p>
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

        <form
          className="mt-5 space-y-6"
          onSubmit={(event) => {
            event.preventDefault();
            if (!dirty) return;
            saveAll.mutate(changes);
          }}
        >
          {groupedFields(specs).map((group) => (
            <fieldset key={group.key}>
              <legend className="text-xs font-medium text-neutral-500">{group.label}</legend>
              <div className="mt-3 space-y-4">
                {group.fields.map((spec) => (
                  <ProfileField
                    draft={draft[spec.path] ?? ""}
                    key={spec.path}
                    onChange={(value) => updateField(spec.path, value)}
                    spec={spec}
                  />
                ))}
              </div>
            </fieldset>
          ))}

          <div className="sticky bottom-0 flex flex-wrap items-center justify-between gap-2 border-t border-neutral-200 bg-white/95 py-3 backdrop-blur">
            <p className="text-[11px] text-neutral-500">
              {dirty ? "有未保存的修改；留空的字段保存后会被删除。" : "没有未保存的修改。"}
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                disabled={saveAll.isPending || clearAll.isPending}
                onClick={() => {
                  if (!window.confirm("确定清空整份画像？清空后无法恢复。")) return;
                  clearAll.mutate();
                }}
                size="sm"
                type="button"
                variant="danger"
              >
                清空画像
              </Button>
              <Button
                disabled={!dirty || saveAll.isPending}
                onClick={resetDraft}
                size="sm"
                type="button"
              >
                撤销修改
              </Button>
              <Button disabled={!dirty || saveAll.isPending} size="sm" type="submit" variant="primary">
                {saveAll.isPending ? "保存中…" : "保存修改"}
              </Button>
            </div>
          </div>
        </form>
      </div>
    </div>
  );
}

function ProfileField({
  spec,
  draft,
  onChange,
}: {
  spec: ProfileFieldSpec;
  draft: string;
  onChange: (value: string) => void;
}) {
  const id = "profile-" + spec.path;
  const hint =
    spec.kind === "string_list"
      ? `多个值用顿号分隔，最多 ${spec.max_items} 项`
      : spec.kind === "multi_choice"
        ? "可多选"
        : spec.description;
  const suffix = spec.explicit_only ? " · 仅在你明确要求记录时写入" : "";

  if (spec.kind === "multi_choice") {
    return (
      <fieldset>
        <legend className="block text-xs font-medium text-neutral-600">{spec.label}</legend>
        <div className="mt-2 flex flex-wrap gap-3">
          {spec.options.map((option) => {
            const picked = splitItems(draft).includes(option.value);
            return (
              <label
                className="flex items-center gap-1.5 text-sm text-neutral-700"
                key={option.value}
              >
                <input
                  checked={picked}
                  onChange={() => {
                    const current = new Set(splitItems(draft));
                    if (current.has(option.value)) current.delete(option.value);
                    else current.add(option.value);
                    onChange(Array.from(current).join(", "));
                  }}
                  type="checkbox"
                />
                {option.label}
              </label>
            );
          })}
        </div>
        <p className="mt-1 text-[11px] text-neutral-400">
          {hint}
          {suffix}
        </p>
      </fieldset>
    );
  }

  return (
    <div>
      <label className="block text-xs font-medium text-neutral-600" htmlFor={id}>
        {spec.label}
      </label>
      <div className="mt-1.5">
        {spec.kind === "choice" || spec.kind === "language" ? (
          <select
            className="block h-9 w-full rounded-md border border-neutral-300 bg-white px-3 text-sm text-neutral-900"
            id={id}
            onChange={(event) => onChange(event.target.value)}
            value={draft}
          >
            <option value="">暂不设置</option>
            {draft && !spec.options.some((option) => option.value === draft) ? (
              // 后端允许任意 BCP-47 标签：已有值不在下拉选项里时也要能显示出来
              <option value={draft}>{draft}</option>
            ) : null}
            {spec.options.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        ) : (
          <input
            className="block h-9 w-full rounded-md border border-neutral-300 px-3 text-sm text-neutral-900 placeholder:text-neutral-400"
            id={id}
            maxLength={spec.kind === "string_list" ? undefined : spec.max_length}
            onChange={(event) => onChange(event.target.value)}
            placeholder={spec.placeholder}
            type="text"
            value={draft}
          />
        )}
      </div>
      <p className="mt-1 text-[11px] text-neutral-400">
        {hint}
        {suffix}
      </p>
    </div>
  );
}
