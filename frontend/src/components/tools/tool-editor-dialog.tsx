"use client";

import { useEffect, useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { CloseIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import {
  createTool,
  getTool,
  ToolApiError,
  updateTool,
  type ContextForgeToolDetail,
  type ToolIntegrationType,
  type ToolWriteInput,
} from "@/core/api/tools";

/**
 * 新建/编辑 ContextForge 工具。
 *
 * 两个约束直接来自网关契约：
 * - 敏感 header 在详情里已经是 ****，提交时必须原样发回，网关才知道"保留原值"；
 * - input_schema 在网关侧是对象，编辑器里是 JSON 文本，解析失败要留在本地拦住。
 */

export interface ToolEditorDialogProps {
  /** 为空表示新建。 */
  toolId?: string | null;
  /** 打开编辑时的列表行，详情加载期间先用它显示名字。 */
  initialTool?: { name: string } | null;
  onClose: () => void;
  onSaved: (tool: ContextForgeToolDetail) => void;
}

interface HeaderRow {
  id: number;
  name: string;
  value: string;
}

interface FormState {
  annotations: string;
  description: string;
  displayName: string;
  enabled: boolean;
  headers: HeaderRow[];
  inputSchema: string;
  integrationType: ToolIntegrationType;
  name: string;
  requestType: string;
  tags: string;
  url: string;
  visibility: "" | "private" | "team" | "public";
}

type FieldErrors = Partial<
  Record<"annotations" | "description" | "input_schema" | "name" | "url", string>
>;

const INPUT_CLASS =
  "h-9 w-full rounded-md border border-neutral-300 bg-white px-3 text-sm text-neutral-900 focus:border-neutral-500 focus:outline-none disabled:bg-neutral-50";
const TEXTAREA_CLASS =
  "w-full rounded-md border border-neutral-300 bg-white px-3 py-2 font-mono text-xs leading-relaxed text-neutral-900 focus:border-neutral-500 focus:outline-none disabled:bg-neutral-50";

let headerSeq = 0;

function nextHeaderId(): number {
  headerSeq += 1;
  return headerSeq;
}

function rowsFromHeaders(headers: Record<string, string>): HeaderRow[] {
  return Object.entries(headers).map(([name, value]) => ({
    id: nextHeaderId(),
    name,
    value,
  }));
}

function formFromTool(tool: ContextForgeToolDetail | null): FormState {
  if (!tool) {
    return {
      annotations: "",
      description: "",
      displayName: "",
      enabled: true,
      headers: [],
      inputSchema: "",
      integrationType: "REST",
      name: "",
      requestType: "SSE",
      tags: "",
      url: "",
      visibility: "",
    };
  }
  return {
    annotations:
      Object.keys(tool.annotations).length > 0
        ? JSON.stringify(tool.annotations, null, 2)
        : "",
    description: tool.description,
    displayName: tool.display_name ?? "",
    enabled: tool.enabled,
    headers: rowsFromHeaders(tool.headers),
    inputSchema:
      Object.keys(tool.input_schema).length > 0
        ? JSON.stringify(tool.input_schema, null, 2)
        : "",
    integrationType: tool.integration_type,
    name: tool.name,
    requestType: tool.request_type,
    tags: tool.tags.join(", "),
    url: tool.url ?? "",
    visibility:
      tool.visibility === "private" ||
      tool.visibility === "team" ||
      tool.visibility === "public"
        ? tool.visibility
        : "",
  };
}

function parseObject(text: string): { error?: string; value?: Record<string, unknown> } {
  if (!text.trim()) return { value: {} };
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { error: "必须是 JSON 对象" };
    }
    return { value: parsed as Record<string, unknown> };
  } catch {
    return { error: "不是合法 JSON" };
  }
}

function readableError(error: unknown): string {
  if (error instanceof ToolApiError) return error.detail;
  if (error instanceof Error) return error.message;
  return String(error);
}

/** 把网关 "input_schema: must be an object" 的前缀映射回具体字段。 */
function fieldFromDetail(detail: string): keyof FieldErrors | null {
  const match = /^(name|description|url|input_schema|annotations)\s*:/.exec(detail);
  return match?.[1] ? (match[1] as keyof FieldErrors) : null;
}

export function ToolEditorDialog({
  toolId = null,
  initialTool = null,
  onClose,
  onSaved,
}: ToolEditorDialogProps) {
  const editing = Boolean(toolId);
  const detailQuery = useQuery({
    queryKey: ["tools", "detail", toolId],
    queryFn: () => getTool(toolId as string),
    enabled: editing,
    staleTime: 5_000,
  });

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const title = editing ? "编辑工具" : "新建工具";
  const titleName = detailQuery.data?.name ?? initialTool?.name ?? toolId ?? "";

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-neutral-900/40 px-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        aria-label={title}
        aria-modal="true"
        className="flex max-h-[90vh] w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-neutral-200 bg-white shadow-xl"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
      >
        <header className="flex shrink-0 items-center border-b border-neutral-200 px-5 py-3">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-neutral-900">{title}</h2>
            {editing && titleName ? (
              <p className="truncate text-[11px] text-neutral-500">{titleName}</p>
            ) : null}
          </div>
          <button
            aria-label="关闭"
            className="ml-auto rounded p-1 text-neutral-500 transition-colors hover:bg-neutral-100 hover:text-neutral-900"
            onClick={onClose}
            type="button"
          >
            <CloseIcon size={16} />
          </button>
        </header>

        {editing && detailQuery.isLoading ? (
          <div className="space-y-3 px-5 py-5" aria-busy="true" aria-label="加载工具详情">
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : null}

        {editing && detailQuery.error ? (
          <div className="px-5 py-5">
            <p
              className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
              role="alert"
            >
              {readableError(detailQuery.error)}
            </p>
          </div>
        ) : null}

        {!editing ? (
          <ToolEditorForm
            initial={null}
            onClose={onClose}
            onSaved={onSaved}
            toolId={null}
          />
        ) : detailQuery.data ? (
          <ToolEditorForm
            initial={detailQuery.data}
            key={detailQuery.data.id + ":" + (detailQuery.data.updated_at ?? "")}
            onClose={onClose}
            onSaved={onSaved}
            toolId={detailQuery.data.id}
          />
        ) : null}
      </div>
    </div>
  );
}

interface ToolEditorFormProps {
  initial: ContextForgeToolDetail | null;
  onClose: () => void;
  onSaved: (tool: ContextForgeToolDetail) => void;
  toolId: string | null;
}

function ToolEditorForm({ initial, onClose, onSaved, toolId }: ToolEditorFormProps) {
  const baseId = useId();
  const [form, setForm] = useState<FormState>(() => formFromTool(initial));
  const [fieldErrors, setFieldErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
  };

  const updateHeader = (id: number, patch: Partial<HeaderRow>) => {
    setForm((current) => ({
      ...current,
      headers: current.headers.map((row) => (row.id === id ? { ...row, ...patch } : row)),
    }));
  };

  const addHeader = () => {
    setForm((current) => ({
      ...current,
      headers: [...current.headers, { id: nextHeaderId(), name: "", value: "" }],
    }));
  };

  const removeHeader = (id: number) => {
    setForm((current) => ({
      ...current,
      headers: current.headers.filter((row) => row.id !== id),
    }));
  };

  const submit = async () => {
    if (busy) return;
    const errors: FieldErrors = {};
    if (!form.name.trim()) errors.name = "请填写工具名称";
    else if (!/^[A-Za-z0-9_-]{1,64}$/.test(form.name.trim())) {
      errors.name = "只能包含字母、数字、下划线和短横线，最多 64 个字符";
    }
    if (!form.description.trim()) errors.description = "请填写描述";
    if (form.url.trim() && !/^https?:\/\//i.test(form.url.trim())) {
      errors.url = "地址必须以 http:// 或 https:// 开头";
    }
    const schema = parseObject(form.inputSchema);
    if (schema.error) errors.input_schema = "输入 Schema " + schema.error;
    const annotations = parseObject(form.annotations);
    if (annotations.error) errors.annotations = "备注 " + annotations.error;
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;

    const headers: Record<string, string> = {};
    for (const row of form.headers) {
      const name = row.name.trim();
      if (!name) continue;
      headers[name] = row.value;
    }

    const payload: ToolWriteInput = {
      name: form.name.trim(),
      display_name: form.displayName.trim() || null,
      description: form.description.trim(),
      integration_type: form.integrationType,
      request_type: form.requestType as ToolWriteInput["request_type"],
      url: form.url.trim() || null,
      headers,
      input_schema: schema.value ?? {},
      annotations: annotations.value ?? {},
      tags: form.tags
        .split(/[,，]/)
        .map((tag) => tag.trim())
        .filter(Boolean),
      visibility: form.visibility || null,
      enabled: form.enabled,
    };

    setBusy(true);
    setFormError(null);
    try {
      const saved = toolId
        ? await updateTool(toolId, payload)
        : await createTool(payload);
      onSaved(saved);
    } catch (cause) {
      const detail = readableError(cause);
      setFormError(detail);
      if (cause instanceof ToolApiError) {
        const field = fieldFromDetail(cause.detail);
        if (field) {
          setFieldErrors({
            [field]: cause.detail.slice(cause.detail.indexOf(":") + 1).trim(),
          });
        }
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-5"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-neutral-700">工具名称</span>
          <input
            aria-describedby={fieldErrors.name ? baseId + "-name-error" : undefined}
            aria-invalid={Boolean(fieldErrors.name)}
            className={INPUT_CLASS}
            disabled={busy}
            maxLength={64}
            onChange={(event) => set("name", event.target.value)}
            placeholder="例如：gitlab_search"
            value={form.name}
          />
          {fieldErrors.name ? (
            <span className="block text-[11px] text-red-600" id={baseId + "-name-error"}>
              {fieldErrors.name}
            </span>
          ) : null}
        </label>

        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-neutral-700">显示名称</span>
          <input
            className={INPUT_CLASS}
            disabled={busy}
            maxLength={255}
            onChange={(event) => set("displayName", event.target.value)}
            placeholder="可选，给人看的名字"
            value={form.displayName}
          />
        </label>
      </div>

      <label className="block space-y-1.5">
        <span className="text-xs font-medium text-neutral-700">描述</span>
        <textarea
          aria-describedby={fieldErrors.description ? baseId + "-description-error" : undefined}
          aria-invalid={Boolean(fieldErrors.description)}
          className={TEXTAREA_CLASS}
          disabled={busy}
          onChange={(event) => set("description", event.target.value)}
          placeholder="这个工具做什么、什么时候该用它"
          rows={3}
          value={form.description}
        />
        {fieldErrors.description ? (
          <span className="block text-[11px] text-red-600" id={baseId + "-description-error"}>
            {fieldErrors.description}
          </span>
        ) : null}
      </label>

      <div className="grid gap-4 sm:grid-cols-3">
        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-neutral-700">集成类型</span>
          <select
            className={INPUT_CLASS}
            disabled={busy}
            onChange={(event) => set("integrationType", event.target.value as ToolIntegrationType)}
            value={form.integrationType}
          >
            <option value="REST">REST</option>
            <option value="MCP">MCP</option>
            <option value="A2A">A2A</option>
          </select>
        </label>

        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-neutral-700">请求方法</span>
          <select
            className={INPUT_CLASS}
            disabled={busy}
            onChange={(event) => set("requestType", event.target.value)}
            value={form.requestType}
          >
            {["SSE", "GET", "POST", "PUT", "DELETE", "PATCH"].map((method) => (
              <option key={method} value={method}>
                {method}
              </option>
            ))}
          </select>
        </label>

        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-neutral-700">可见性</span>
          <select
            className={INPUT_CLASS}
            disabled={busy}
            onChange={(event) =>
              set("visibility", event.target.value as FormState["visibility"])
            }
            value={form.visibility}
          >
            <option value="">默认</option>
            <option value="private">私有</option>
            <option value="team">团队</option>
            <option value="public">公开</option>
          </select>
        </label>
      </div>

      <label className="block space-y-1.5">
        <span className="text-xs font-medium text-neutral-700">工具地址</span>
        <input
          aria-describedby={fieldErrors.url ? baseId + "-url-error" : undefined}
          aria-invalid={Boolean(fieldErrors.url)}
          className={INPUT_CLASS}
          disabled={busy}
          onChange={(event) => set("url", event.target.value)}
          placeholder="https://..."
          value={form.url}
        />
        {fieldErrors.url ? (
          <span className="block text-[11px] text-red-600" id={baseId + "-url-error"}>
            {fieldErrors.url}
          </span>
        ) : null}
      </label>

      <label className="block space-y-1.5">
        <span className="text-xs font-medium text-neutral-700">标签</span>
        <input
          className={INPUT_CLASS}
          disabled={busy}
          onChange={(event) => set("tags", event.target.value)}
          placeholder="用逗号分隔，例如：gitlab, search"
          value={form.tags}
        />
      </label>

      <fieldset className="space-y-2">
        <legend className="flex w-full items-center justify-between gap-3">
          <span className="text-xs font-medium text-neutral-700">请求头</span>
          <Button disabled={busy} onClick={addHeader} size="sm" type="button">
            添加请求头
          </Button>
        </legend>
        <p className="text-[11px] leading-relaxed text-neutral-500">
          敏感值由网关掩码为 ****；保持 **** 不变即可沿用已保存的真实值。
        </p>
        {form.headers.length === 0 ? (
          <p className="rounded-md border border-dashed border-neutral-300 px-3 py-3 text-[11px] text-neutral-400">
            没有请求头
          </p>
        ) : (
          <div className="space-y-2">
            {form.headers.map((row, index) => (
              <div className="flex items-center gap-2" key={row.id}>
                <input
                  aria-label={"请求头名称 " + (index + 1)}
                  className={INPUT_CLASS}
                  disabled={busy}
                  onChange={(event) => updateHeader(row.id, { name: event.target.value })}
                  placeholder="Authorization"
                  value={row.name}
                />
                <input
                  aria-label={"请求头值 " + (index + 1)}
                  className={INPUT_CLASS}
                  disabled={busy}
                  onChange={(event) => updateHeader(row.id, { value: event.target.value })}
                  placeholder="Bearer ****"
                  value={row.value}
                />
                <Button
                  aria-label={"删除请求头 " + (index + 1)}
                  disabled={busy}
                  onClick={() => removeHeader(row.id)}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  删除
                </Button>
              </div>
            ))}
          </div>
        )}
      </fieldset>

      <label className="block space-y-1.5">
        <span className="text-xs font-medium text-neutral-700">输入 Schema（JSON）</span>
        <textarea
          aria-describedby={fieldErrors.input_schema ? baseId + "-schema-error" : undefined}
          aria-invalid={Boolean(fieldErrors.input_schema)}
          className={TEXTAREA_CLASS}
          disabled={busy}
          onChange={(event) => set("inputSchema", event.target.value)}
          placeholder={'{"type":"object","properties":{}}'}
          rows={7}
          value={form.inputSchema}
        />
        {fieldErrors.input_schema ? (
          <span className="block text-[11px] text-red-600" id={baseId + "-schema-error"}>
            {fieldErrors.input_schema}
          </span>
        ) : null}
      </label>

      <label className="block space-y-1.5">
        <span className="text-xs font-medium text-neutral-700">备注（JSON，可选）</span>
        <textarea
          aria-describedby={fieldErrors.annotations ? baseId + "-annotations-error" : undefined}
          aria-invalid={Boolean(fieldErrors.annotations)}
          className={TEXTAREA_CLASS}
          disabled={busy}
          onChange={(event) => set("annotations", event.target.value)}
          placeholder={'{"readOnlyHint":true}'}
          rows={4}
          value={form.annotations}
        />
        {fieldErrors.annotations ? (
          <span className="block text-[11px] text-red-600" id={baseId + "-annotations-error"}>
            {fieldErrors.annotations}
          </span>
        ) : null}
      </label>

      <label className="flex items-center gap-2 text-xs text-neutral-700">
        <input
          checked={form.enabled}
          disabled={busy}
          onChange={(event) => set("enabled", event.target.checked)}
          type="checkbox"
        />
        创建后立即启用
      </label>

      {formError ? (
        <p
          className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
          role="alert"
        >
          {formError}
        </p>
      ) : null}

      <div className="sticky bottom-0 flex justify-end gap-2 border-t border-neutral-100 bg-white pt-4">
        <Button disabled={busy} onClick={onClose} size="sm" type="button">
          取消
        </Button>
        <Button disabled={busy} size="sm" type="submit" variant="primary">
          {busy ? "保存中…" : toolId ? "保存" : "创建"}
        </Button>
      </div>
    </form>
  );
}