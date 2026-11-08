"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { CloseIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import {
  McpServerApiError,
  createMcpServer,
  getMcpServer,
  updateMcpServer,
  type McpServerDetail,
  type McpServerInput,
  type McpServerUpdateInput,
  type McpTransport,
} from "@/core/api/mcp-servers";

/**
 * 新建/编辑 MCP 服务。
 *
 * 三条约束直接来自网关契约：
 * - name 是 slug，创建后不可改（它被烧进每个工具调用名），编辑时只读；
 * - enabled 走 /state，PUT 不接受它，所以编辑表单里没有这个开关；
 * - headers 只有掩码 ****，提交时原样发回，网关才知道"保留原值"。
 *
 * URL 只回传去掉查询串的部分：用户不写查询串就提交时，网关沿用已保存的那段，
 * 所以 url_has_query 时要明说一句，否则看起来像"地址被截断了"。
 */

export interface McpServerEditorDialogProps {
  /** 为空表示新建。 */
  serverId?: string | null;
  /** 打开编辑时的列表行，详情加载期间先用它显示名字。 */
  initialServer?: { name: string } | null;
  onClose: () => void;
  onSaved: (server: McpServerDetail) => void;
}

interface HeaderRow {
  id: number;
  name: string;
  value: string;
}

interface FormState {
  description: string;
  displayName: string;
  enabled: boolean;
  headers: HeaderRow[];
  name: string;
  transport: McpTransport;
  url: string;
  verifyTls: boolean;
}

type FieldErrors = Partial<Record<"name" | "url", string>>;

const INPUT_CLASS =
  "h-9 w-full rounded-md border border-neutral-300 bg-white px-3 text-sm text-neutral-900 focus:border-neutral-500 focus:outline-none disabled:bg-neutral-50 disabled:text-neutral-500";
const TEXTAREA_CLASS =
  "w-full rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm leading-relaxed text-neutral-900 focus:border-neutral-500 focus:outline-none disabled:bg-neutral-50";

/** 传输方式只有两种；标签用 MCP 规范里的写法，免得中英混排时认错。 */
const TRANSPORT_OPTIONS: readonly { label: string; value: McpTransport }[] = [
  { label: "STREAMABLEHTTP", value: "streamable_http" },
  { label: "SSE", value: "sse" },
];

let headerSeq = 0;

function nextHeaderId(): number {
  headerSeq += 1;
  return headerSeq;
}

function rowsFromHeaders(headers: Record<string, string>): HeaderRow[] {
  return Object.entries(headers).map(([name, value]) => ({ id: nextHeaderId(), name, value }));
}

function formFromServer(server: McpServerDetail | null): FormState {
  if (!server) {
    return {
      description: "",
      displayName: "",
      enabled: true,
      headers: [],
      name: "",
      transport: "streamable_http",
      url: "",
      verifyTls: true,
    };
  }
  return {
    description: server.description,
    displayName: server.display_name ?? "",
    enabled: server.enabled,
    headers: rowsFromHeaders(server.headers),
    name: server.name,
    transport: server.transport,
    url: server.url,
    verifyTls: server.verify_tls,
  };
}

function readableError(error: unknown): string {
  if (error instanceof McpServerApiError) return error.detail;
  if (error instanceof Error) return error.message;
  return String(error);
}

/** 把网关 "url: ..." / "name: ..." 的前缀映射回具体字段。 */
function fieldFromDetail(detail: string): keyof FieldErrors | null {
  const match = /^(name|url)\s*:/.exec(detail);
  return match?.[1] ? (match[1] as keyof FieldErrors) : null;
}

export function McpServerEditorDialog({
  serverId = null,
  initialServer = null,
  onClose,
  onSaved,
}: McpServerEditorDialogProps) {
  const editing = Boolean(serverId);
  const detailQuery = useQuery({
    queryKey: ["mcp-servers", serverId],
    queryFn: () => getMcpServer(serverId as string),
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

  const title = editing ? "编辑 MCP 服务" : "新建 MCP 服务";
  const titleName = detailQuery.data?.name ?? initialServer?.name ?? serverId ?? "";

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
          <div className="space-y-3 px-5 py-5" aria-busy="true" aria-label="加载 MCP 服务详情">
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
          <McpServerEditorForm
            initial={null}
            onClose={onClose}
            onSaved={onSaved}
            serverId={null}
          />
        ) : detailQuery.data ? (
          <McpServerEditorForm
            initial={detailQuery.data}
            key={detailQuery.data.id + ":" + (detailQuery.data.updated_at ?? "")}
            onClose={onClose}
            onSaved={onSaved}
            serverId={detailQuery.data.id}
          />
        ) : null}
      </div>
    </div>
  );
}

interface McpServerEditorFormProps {
  initial: McpServerDetail | null;
  onClose: () => void;
  onSaved: (server: McpServerDetail) => void;
  serverId: string | null;
}

function McpServerEditorForm({ initial, onClose, onSaved, serverId }: McpServerEditorFormProps) {
  const baseId = useId();
  const [form, setForm] = useState<FormState>(() => formFromServer(initial));
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
    const name = form.name.trim();
    const url = form.url.trim();
    if (!name) errors.name = "请填写服务名称";
    else if (!/^[a-z0-9-]{1,128}$/.test(name)) {
      errors.name = "只能包含小写字母、数字和短横线，最多 128 个字符";
    }
    if (!/^https?:\/\//i.test(url)) errors.url = "服务地址必须以 http:// 或 https:// 开头";
    setFieldErrors(errors);
    if (Object.keys(errors).length > 0) return;

    const headers: Record<string, string> = {};
    for (const row of form.headers) {
      const headerName = row.name.trim();
      if (!headerName) continue;
      headers[headerName] = row.value;
    }

    setBusy(true);
    setFormError(null);
    try {
      const saved = serverId
        ? await updateMcpServer(serverId, {
            display_name: form.displayName.trim() || null,
            description: form.description.trim(),
            url,
            transport: form.transport,
            headers,
            verify_tls: form.verifyTls,
          } satisfies McpServerUpdateInput)
        : await createMcpServer({
            name,
            display_name: form.displayName.trim() || null,
            description: form.description.trim(),
            url,
            transport: form.transport,
            headers,
            verify_tls: form.verifyTls,
            enabled: form.enabled,
          } satisfies McpServerInput);
      onSaved(saved);
    } catch (cause) {
      setFormError(readableError(cause));
      if (cause instanceof McpServerApiError) {
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

  const urlHint =
    initial && initial.url_has_query && !form.url.includes("?")
      ? "已保存的 URL 带查询串（内容不回传）；这里的地址不写查询串时会沿用已保存的那段。"
      : null;

  return (
    <form
      className="min-h-0 flex-1 space-y-5 overflow-y-auto px-5 py-5"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="block space-y-1.5">
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-neutral-700">服务名称</span>
            <input
              aria-describedby={fieldErrors.name ? baseId + "-name-error" : undefined}
              aria-invalid={Boolean(fieldErrors.name)}
              className={INPUT_CLASS}
              disabled={busy || Boolean(serverId)}
              maxLength={128}
              onChange={(event) => set("name", event.target.value)}
              placeholder="例如：anysearch"
              value={form.name}
            />
          </label>
          {serverId ? (
            <span className="block text-[11px] text-neutral-400">
              服务名称创建后不可改：它已经写进每个工具的调用名里。
            </span>
          ) : null}
          {fieldErrors.name ? (
            <span className="block text-[11px] text-red-600" id={baseId + "-name-error"}>
              {fieldErrors.name}
            </span>
          ) : null}
        </div>

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
          className={TEXTAREA_CLASS}
          disabled={busy}
          onChange={(event) => set("description", event.target.value)}
          placeholder="这个服务器提供什么工具、什么场景该用它"
          rows={2}
          value={form.description}
        />
      </label>

      <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_12rem]">
        <div className="block space-y-1.5">
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-neutral-700">服务地址</span>
            <input
              aria-describedby={fieldErrors.url ? baseId + "-url-error" : undefined}
              aria-invalid={Boolean(fieldErrors.url)}
              className={INPUT_CLASS}
              disabled={busy}
              onChange={(event) => set("url", event.target.value)}
              placeholder="https://api.example.com/mcp"
              value={form.url}
            />
          </label>
          {urlHint ? (
            <span className="block text-[11px] leading-relaxed text-neutral-500">{urlHint}</span>
          ) : null}
          {fieldErrors.url ? (
            <span className="block text-[11px] text-red-600" id={baseId + "-url-error"}>
              {fieldErrors.url}
            </span>
          ) : null}
        </div>

        <label className="block space-y-1.5">
          <span className="text-xs font-medium text-neutral-700">传输方式</span>
          <select
            className={INPUT_CLASS}
            disabled={busy}
            onChange={(event) => set("transport", event.target.value as McpTransport)}
            value={form.transport}
          >
            {TRANSPORT_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <fieldset className="space-y-2">
        <legend className="flex w-full items-center justify-between gap-3">
          <span className="text-xs font-medium text-neutral-700">请求头</span>
          <Button disabled={busy} onClick={addHeader} size="sm" type="button">
            添加请求头
          </Button>
        </legend>
        <p className="text-[11px] leading-relaxed text-neutral-500">
          敏感值由网关掩码为 ****；保持 **** 不变即沿用已保存的真实值，前端不会显示明文。
        </p>
        {form.headers.length === 0 ? (
          <p className="rounded-md border border-dashed border-neutral-300 px-3 py-3 text-[11px] text-neutral-400">
            没有请求头（不需要认证）
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

      <label className="flex items-center gap-2 text-xs text-neutral-700">
        <input
          checked={form.verifyTls}
          disabled={busy}
          onChange={(event) => set("verifyTls", event.target.checked)}
          type="checkbox"
        />
        校验 TLS 证书
      </label>
      <p className="text-[11px] leading-relaxed text-neutral-500">
        关闭校验只给自签名证书的内网服务用；公网服务请保持开启。
      </p>

      {serverId ? (
        <p className="text-[11px] leading-relaxed text-neutral-500">
          启用状态在服务列表里切换（走 state 接口），这里不提供保存到 PUT。
        </p>
      ) : (
        <label className="flex items-center gap-2 text-xs text-neutral-700">
          <input
            checked={form.enabled}
            disabled={busy}
            onChange={(event) => set("enabled", event.target.checked)}
            type="checkbox"
          />
          创建后立即启用
        </label>
      )}

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
          {busy ? "保存中…" : serverId ? "保存" : "创建"}
        </Button>
      </div>
    </form>
  );
}
