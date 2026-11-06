"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { WrenchIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import {
  deleteTool,
  getToolsStatus,
  listTools,
  previewToolSearch,
  reindexTools,
  setToolEnabled,
  ToolApiError,
  type ContextForgeTool,
  type ToolStatus,
} from "@/core/api/tools";
import { useSession } from "@/core/auth/use-session";
import { cn } from "@/lib/utils";

import { ToolEditorDialog } from "./tool-editor-dialog";

/**
 * 「工具管理」：ContextForge 工具的统一入口。
 *
 * 管理动作只写 ContextForge，异步刷新由网关负责；页面不做本地乐观表，
 * 只把 /tools/status 的每一条链路（管理事件、同步、索引、召回通道）画出来，
 * 让"改了但还没生效"是可解释的，而不是看起来像丢数据。
 */

const PAGE_ERROR_CLASS =
  "rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700";

function readableError(error: unknown): string {
  if (error instanceof ToolApiError) return error.detail;
  if (error instanceof Error) return error.message;
  return String(error);
}

function statusVariant(value: string): "success" | "warning" | "danger" | "neutral" {
  if (value === "ready" || value === "connected" || value === "idle") return "success";
  if (value === "degraded" || value === "connecting" || value === "reconnecting") return "warning";
  if (value === "error" || value === "unavailable") return "danger";
  return "neutral";
}

function adminEventsLabel(value: string): string {
  switch (value) {
    case "connected":
      return "管理事件已连接";
    case "connecting":
      return "管理事件连接中";
    case "reconnecting":
      return "管理事件重连中";
    case "error":
      return "管理事件异常";
    default:
      return "管理事件已禁用";
  }
}

interface EditorState {
  /** null 表示新建。 */
  toolId: string | null;
  tool: ContextForgeTool | null;
}

export function ToolManagementPage() {
  const queryClient = useQueryClient();
  const { status: sessionStatus, user, loading: sessionLoading } = useSession();
  const isAdmin =
    sessionStatus === "disabled" ||
    (sessionStatus === "authenticated" && user?.system_role === "admin");

  const [filter, setFilter] = useState("");
  const [appliedFilter, setAppliedFilter] = useState("");
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [deleting, setDeleting] = useState<ContextForgeTool | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [previewText, setPreviewText] = useState("");

  const listQuery = useQuery({
    queryKey: ["tools", "list", appliedFilter],
    queryFn: () => listTools({ q: appliedFilter || undefined, limit: 100 }),
    enabled: isAdmin,
    staleTime: 5_000,
  });
  const statusQuery = useQuery({
    queryKey: ["tools", "status"],
    queryFn: getToolsStatus,
    enabled: isAdmin,
    staleTime: 5_000,
  });
  const preview = useMutation({
    mutationFn: (q: string) => previewToolSearch(q, 5),
  });
  const reindex = useMutation({
    mutationFn: reindexTools,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["tools"] });
    },
  });

  const refreshAll = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["tools"] }),
      listQuery.refetch(),
      statusQuery.refetch(),
    ]);
  };

  if (sessionLoading) {
    return (
      <div className="h-full bg-white px-5 py-6 sm:px-8" aria-busy="true" aria-label="加载会话">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="mt-4 h-24 w-full" />
      </div>
    );
  }

  if (!isAdmin) {
    return (
      <div className="flex h-full items-center justify-center bg-white px-5 py-10">
        <div className="max-w-md rounded-xl border border-neutral-200 bg-neutral-50 px-6 py-8 text-center">
          <WrenchIcon className="mx-auto text-neutral-300" size={28} />
          <h1 className="mt-3 text-lg font-semibold text-neutral-900">无权访问</h1>
          <p className="mt-2 text-xs leading-relaxed text-neutral-500">
            只有管理员可以管理工具。工具管理和 ContextForge 凭据都属于系统管理员。
          </p>
        </div>
      </div>
    );
  }

  const tools = listQuery.data?.tools ?? [];
  const loading = listQuery.isLoading;
  const listError = listQuery.error;
  const status = statusQuery.data;
  const empty = !loading && !listError && tools.length === 0;
  const error = listError ?? statusQuery.error;

  const toggle = async (tool: ContextForgeTool) => {
    if (busyId) return;
    setBusyId(tool.id);
    setActionError(null);
    try {
      await setToolEnabled(tool.id, !tool.enabled);
      await queryClient.invalidateQueries({ queryKey: ["tools"] });
    } catch (cause) {
      setActionError(readableError(cause));
    } finally {
      setBusyId(null);
    }
  };

  const confirmDelete = async () => {
    if (!deleting || busyId) return;
    setBusyId(deleting.id);
    setActionError(null);
    try {
      await deleteTool(deleting.id);
      setDeleting(null);
      await queryClient.invalidateQueries({ queryKey: ["tools"] });
    } catch (cause) {
      setActionError(readableError(cause));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto w-full max-w-6xl px-5 py-6 sm:px-8">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-neutral-200 pb-5">
          <div>
            <div className="flex items-center gap-2">
              <WrenchIcon className="text-neutral-500" size={18} />
              <h1 className="text-xl font-semibold tracking-tight text-neutral-900">工具管理</h1>
            </div>
            <p className="mt-1 text-xs leading-relaxed text-neutral-500">
              工具写在 ContextForge；MyFlow 异步同步到运行时与 Milvus。提示「尚未生效」时稍等状态栏刷新。
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={listQuery.isFetching || statusQuery.isFetching}
              onClick={() => void refreshAll()}
              size="sm"
              type="button"
            >
              刷新
            </Button>
            <Button
              disabled={reindex.isPending}
              onClick={() => reindex.mutate()}
              size="sm"
              type="button"
            >
              {reindex.isPending ? "重建中…" : "重建索引"}
            </Button>
            <Button
              onClick={() => setEditor({ toolId: null, tool: null })}
              size="sm"
              type="button"
              variant="primary"
            >
              新建工具
            </Button>
          </div>
        </header>

        <ToolStatusBar
          adminEvents={status?.admin_events ?? "disabled"}
          error={statusQuery.error ? readableError(statusQuery.error) : null}
          loading={statusQuery.isLoading}
          status={status}
        />

        {reindex.data ? (
          <p className="mt-3 text-[11px] text-neutral-500" role="status">
            扫描 {reindex.data.scanned}，变更 {reindex.data.changed}，写入 {reindex.data.upserted}，
            删除 {reindex.data.deleted}
            {reindex.data.error ? "；错误：" + reindex.data.error : ""}
          </p>
        ) : null}
        {reindex.error ? (
          <p className={cn(PAGE_ERROR_CLASS, "mt-3")} role="alert">
            {readableError(reindex.error)}
          </p>
        ) : null}

        <section aria-label="检索预览" className="mt-5 rounded-lg border border-neutral-200 bg-neutral-50/60 p-4">
          <h2 className="text-xs font-medium text-neutral-700">检索预览</h2>
          <p className="mt-1 text-[11px] leading-relaxed text-neutral-500">
            用一句话描述任务，看当前向量 + 关键词 + 重排能不能召回正确的工具。
          </p>
          <form
            className="mt-3 flex flex-wrap gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              const text = previewText.trim();
              if (text) preview.mutate(text);
            }}
          >
            <input
              aria-label="检索预览查询"
              className="h-9 min-w-0 flex-1 rounded-md border border-neutral-300 bg-white px-3 text-sm text-neutral-900 focus:border-neutral-500 focus:outline-none"
              onChange={(event) => setPreviewText(event.target.value)}
              placeholder="例如：帮我搜一个 GitLab 项目"
              value={previewText}
            />
            <Button disabled={preview.isPending || !previewText.trim()} size="sm" type="submit">
              {preview.isPending ? "检索中…" : "预览检索"}
            </Button>
          </form>

          {preview.error ? (
            <p className={cn(PAGE_ERROR_CLASS, "mt-3")} role="alert">
              {readableError(preview.error)}
            </p>
          ) : null}

          {preview.data ? (
            <div aria-label="检索预览结果" className="mt-3" role="region">
              <div className="flex flex-wrap gap-1.5">
                <Badge variant="outline">索引 {preview.data.retrieval.index}</Badge>
                {preview.data.retrieval.channels.map((channel) => (
                  <Badge key={channel} variant="outline">
                    通道 {channel}
                  </Badge>
                ))}
                <Badge variant="outline">候选 {preview.data.retrieval.candidates}</Badge>
                <Badge variant={preview.data.retrieval.reranked ? "success" : "neutral"}>
                  {preview.data.retrieval.reranked ? "已重排" : "未重排"}
                </Badge>
              </div>
              {preview.data.retrieval.warnings.length > 0 ? (
                <ul className="mt-2 space-y-1">
                  {preview.data.retrieval.warnings.map((warning) => (
                    <li className="text-[11px] text-amber-700" key={warning}>
                      {warning}
                    </li>
                  ))}
                </ul>
              ) : null}
              {preview.data.results.length === 0 ? (
                <p className="mt-2 text-xs text-neutral-500">没有召回任何工具。</p>
              ) : (
                <ol className="mt-2 divide-y divide-neutral-200 rounded-md border border-neutral-200 bg-white">
                  {preview.data.results.map((hit) => (
                    <li className="flex items-start gap-3 px-3 py-2" key={hit.tool_name}>
                      <span className="min-w-0 flex-1">
                        <span className="block text-xs font-medium text-neutral-900">
                          {hit.tool_name}
                        </span>
                        <span className="mt-0.5 block text-[11px] leading-relaxed text-neutral-500">
                          {hit.description}
                        </span>
                      </span>
                      <span className="shrink-0 text-[11px] tabular-nums text-neutral-400">
                        {hit.score.toFixed(3)}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
            </div>
          ) : null}
        </section>

        <div className="mt-6 flex flex-wrap items-center gap-2">
          <form
            className="flex min-w-0 flex-1 gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              setAppliedFilter(filter.trim());
            }}
          >
            <input
              aria-label="筛选工具"
              className="h-9 min-w-0 flex-1 rounded-md border border-neutral-300 bg-white px-3 text-sm text-neutral-900 focus:border-neutral-500 focus:outline-none sm:max-w-sm"
              onChange={(event) => setFilter(event.target.value)}
              placeholder="按名称、描述、标签筛选"
              value={filter}
            />
            <Button size="sm" type="submit">
              筛选
            </Button>
          </form>
          <p className="text-[11px] text-neutral-500">{tools.length} 个工具</p>
        </div>

        {actionError ? (
          <p className={cn(PAGE_ERROR_CLASS, "mt-3")} role="alert">
            {actionError}
          </p>
        ) : null}

        {loading ? (
          <div className="mt-4 space-y-3" aria-busy="true" aria-label="加载工具列表">
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-28 w-full" />
            <Skeleton className="h-28 w-full" />
          </div>
        ) : null}

        {error ? (
          <p className={cn(PAGE_ERROR_CLASS, "mt-4")} role="alert">
            {readableError(error)}
          </p>
        ) : null}

        {empty ? (
          <div className="mt-8 rounded-lg border border-dashed border-neutral-300 bg-neutral-50/60 px-6 py-12 text-center">
            <WrenchIcon className="mx-auto text-neutral-300" size={28} />
            <p className="mt-3 text-sm font-medium text-neutral-700">还没有工具</p>
            <p className="mt-1 text-xs text-neutral-500">
              在 ContextForge 里注册工具，或在这里新建一个 REST 工具。
            </p>
            <Button
              className="mt-4"
              onClick={() => setEditor({ toolId: null, tool: null })}
              size="sm"
              type="button"
              variant="primary"
            >
              新建工具
            </Button>
          </div>
        ) : null}

        {tools.length > 0 ? (
          <ul className="mt-4 space-y-3">
            {tools.map((tool) => (
              <li key={tool.id}>
                <ToolRow
                  busy={busyId === tool.id}
                  onDelete={() => setDeleting(tool)}
                  onEdit={() => setEditor({ toolId: tool.id, tool })}
                  onToggle={() => void toggle(tool)}
                  tool={tool}
                />
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      {editor ? (
        <ToolEditorDialog
          initialTool={editor.tool}
          onClose={() => setEditor(null)}
          onSaved={() => {
            setEditor(null);
            void queryClient.invalidateQueries({ queryKey: ["tools"] });
          }}
          toolId={editor.toolId}
        />
      ) : null}

      {deleting ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-neutral-900/40 px-4"
          onClick={() => setDeleting(null)}
          role="presentation"
        >
          <div
            aria-label="删除工具"
            aria-modal="true"
            className="w-full max-w-md rounded-xl border border-neutral-200 bg-white p-5 shadow-xl"
            onClick={(event) => event.stopPropagation()}
            role="dialog"
          >
            <h2 className="text-sm font-semibold text-neutral-900">删除工具</h2>
            <p className="mt-2 text-xs leading-relaxed text-neutral-600">
              确定删除 <strong className="text-neutral-900">{deleting.name}</strong> 吗？ContextForge
              中的定义会一并删除；已加载到运行时的副本会在下次刷新后消失。
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <Button onClick={() => setDeleting(null)} size="sm" type="button">
                取消
              </Button>
              <Button
                disabled={busyId === deleting.id}
                onClick={() => void confirmDelete()}
                size="sm"
                type="button"
                variant="danger"
              >
                {busyId === deleting.id ? "删除中…" : "确认删除"}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

interface ToolStatusBarProps {
  adminEvents: string;
  error: string | null;
  loading: boolean;
  status: ToolStatus | undefined;
}

function ToolStatusBar({ adminEvents, error, loading, status }: ToolStatusBarProps) {
  return (
    <section aria-label="工具状态" className="mt-4 rounded-lg border border-neutral-200 bg-neutral-50/60 p-4">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant={status?.enabled ? "success" : "neutral"}>
          ContextForge {status?.enabled ? "已启用" : "未启用"}
        </Badge>
        <Badge variant={statusVariant(adminEvents)}>{adminEventsLabel(adminEvents)}</Badge>
        <Badge variant={statusVariant(status?.sync ?? "disabled")}>
          同步 {status?.sync ?? "disabled"}
        </Badge>
        <Badge variant={statusVariant(status?.index ?? "disabled")}>
          索引 {status?.index ?? "disabled"}
        </Badge>
        {status ? (
          <>
            <Badge variant={statusVariant(status.channels.keyword)}>
              关键词 {status.channels.keyword}
            </Badge>
            <Badge variant={statusVariant(status.channels.embedding)}>
              向量 {status.channels.embedding}
            </Badge>
            <Badge variant={statusVariant(status.channels.rerank)}>
              重排 {status.channels.rerank}
            </Badge>
            <span className="ml-1 text-[11px] tabular-nums text-neutral-500">
              {status.indexed_tools} / {status.total_tools} 已索引
            </span>
          </>
        ) : null}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-neutral-500">
        {loading ? <span>正在读取状态…</span> : null}
        {status?.last_sync_at ? (
          <span>
            上次同步 {status.last_sync_at}
            {status.last_sync_reason ? "（" + status.last_sync_reason + "）" : ""}
          </span>
        ) : null}
        {status?.reachable === false ? <span className="text-amber-700">ContextForge 不可达</span> : null}
        {status?.auth_ok === false ? <span className="text-amber-700">管理凭据无效</span> : null}
        {status?.last_error ? <span className="text-red-600">{status.last_error}</span> : null}
        {error ? <span className="text-red-600">{error}</span> : null}
      </div>
    </section>
  );
}

interface ToolRowProps {
  busy: boolean;
  onDelete: () => void;
  onEdit: () => void;
  onToggle: () => void;
  tool: ContextForgeTool;
}

function ToolRow({ busy, onDelete, onEdit, onToggle, tool }: ToolRowProps) {
  return (
    <article
      aria-label={tool.name}
      className="rounded-lg border border-neutral-200 bg-white p-4 transition-colors hover:border-neutral-300"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-baseline gap-2">
            <h2 className="truncate text-sm font-semibold text-neutral-900">{tool.name}</h2>
            {tool.display_name ? (
              <span className="truncate text-xs text-neutral-500">{tool.display_name}</span>
            ) : null}
          </div>
          <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-neutral-600">
            {tool.description || "没有描述"}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            aria-label="编辑工具"
            disabled={busy}
            onClick={onEdit}
            size="sm"
            type="button"
            variant="ghost"
          >
            编辑
          </Button>
          <Button
            aria-label="删除工具"
            disabled={busy}
            onClick={onDelete}
            size="sm"
            type="button"
            variant="danger"
          >
            删除
          </Button>
          <button
            aria-checked={tool.enabled}
            aria-label={tool.enabled ? "停用工具" : "启用工具"}
            className={cn(
              "relative h-5 w-9 shrink-0 rounded-full transition-colors",
              tool.enabled ? "bg-emerald-500" : "bg-neutral-300",
              busy && "opacity-50",
            )}
            disabled={busy}
            onClick={onToggle}
            role="switch"
            type="button"
          >
            <span
              className={cn(
                "absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform",
                tool.enabled ? "translate-x-4" : "translate-x-0.5",
              )}
            />
          </button>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <Badge variant={tool.enabled ? "success" : "neutral"}>
          {tool.enabled ? "已启用" : "已停用"}
        </Badge>
        <Badge variant={tool.reachable ? "success" : "danger"}>
          {tool.reachable ? "可达" : "不可达"}
        </Badge>
        <Badge variant="outline">索引 {tool.index_status}</Badge>
        <Badge variant="outline">{tool.integration_type}</Badge>
        {tool.visibility ? <Badge variant="outline">{tool.visibility}</Badge> : null}
        {tool.shadowed_by ? (
          <Badge variant="warning">被 {tool.shadowed_by} 遮蔽</Badge>
        ) : null}
      </div>

      {tool.tags.length > 0 ? (
        <p className="mt-2 text-[11px] text-neutral-400">{tool.tags.join(" · ")}</p>
      ) : null}
    </article>
  );
}
