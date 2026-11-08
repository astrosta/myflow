"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { WrenchIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import {
  McpServerApiError,
  deleteMcpServer,
  getMcpServerStatus,
  listMcpServerTools,
  listMcpServers,
  refreshMcpServer,
  reindexMcpServers,
  setMcpServerEnabled,
  type McpServerStatus,
  type McpServerSummary,
  type McpServerTool,
} from "@/core/api/mcp-servers";
import { cn } from "@/lib/utils";

import { McpServerEditorDialog } from "./mcp-server-editor-dialog";

/**
 * 「MCP 服务」：用户自己的 MCP 服务器与其工具的只读投影。
 *
 * 与旧的「工具管理」相比，这一页**没有任何工具写操作**：工具是服务器 tools/list
 * 的结果，归属、schema、调用名都由服务端决定，前端只负责展示（call_name、参数、
 * 是否可调用/启用）。服务器本身才有写操作：新建/编辑/启停/刷新/删除/重建索引。
 *
 * 缓存分成四块，失效范围就是它们的边界：
 *   ["mcp-servers"]                 列表（tool_count 也在这里）
 *   ["mcp-servers", id]             详情（编辑器用，headers 已掩码）
 *   ["mcp-servers", id, "tools"]    只读工具列表
 *   ["mcp-servers", "status"]       汇总状态（计数、索引、上次同步）
 * "刷新 server" 会改工具与同步时间，"重建索引" 只改索引/同步 —— 两者的失效范围
 * 不一样，混着用的结果就是界面停在旧数字上。
 */

const PAGE_ERROR_CLASS =
  "rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700";

const LIST_KEY = ["mcp-servers"] as const;
const STATUS_KEY = ["mcp-servers", "status"] as const;
const serverKey = (serverId: string) => ["mcp-servers", serverId] as const;
const toolsKey = (serverId: string) => ["mcp-servers", serverId, "tools"] as const;

function readableError(error: unknown): string {
  if (error instanceof McpServerApiError) return error.detail;
  if (error instanceof Error) return error.message;
  return String(error);
}

/** 状态词落到颜色上，运维才看得出"哪一台坏了"而不是"都灰着"。 */
function statusVariant(value: string): "success" | "warning" | "danger" | "neutral" {
  if (value === "active") return "success";
  if (value === "pending") return "warning";
  if (value === "error") return "danger";
  return "neutral";
}

function transportLabel(transport: string): string {
  return transport === "sse" ? "SSE" : "STREAMABLEHTTP";
}

interface EditorState {
  /** null 表示新建。 */
  serverId: string | null;
  server: McpServerSummary | null;
}

export function McpServerManagementPage() {
  const queryClient = useQueryClient();
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [deleting, setDeleting] = useState<McpServerSummary | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: LIST_KEY,
    queryFn: listMcpServers,
    staleTime: 5_000,
  });
  const statusQuery = useQuery({
    queryKey: STATUS_KEY,
    queryFn: getMcpServerStatus,
    staleTime: 5_000,
  });
  const reindex = useMutation({
    mutationFn: reindexMcpServers,
    onSuccess: async () => {
      // 重建索引会重写 tool 文档与同步时间：状态和每一台的工具缓存都不能再用。
      await queryClient.invalidateQueries({ queryKey: STATUS_KEY });
      await queryClient.invalidateQueries({ queryKey: LIST_KEY });
    },
  });

  const servers = listQuery.data?.servers ?? [];
  const loading = listQuery.isLoading;
  const listError = listQuery.error;
  const empty = !loading && !listError && servers.length === 0;
  const error = listError ?? statusQuery.error;

  const refreshAll = async () => {
    await queryClient.invalidateQueries({ queryKey: LIST_KEY });
    await queryClient.invalidateQueries({ queryKey: STATUS_KEY });
  };

  const refreshOne = async (server: McpServerSummary) => {
    if (busyId) return;
    setBusyId(server.id);
    setActionError(null);
    try {
      await refreshMcpServer(server.id);
      // tools/list 变了，server 的 reachable/status/last_error 也变了。
      await queryClient.invalidateQueries({ queryKey: toolsKey(server.id) });
      await queryClient.invalidateQueries({ queryKey: STATUS_KEY });
      await queryClient.invalidateQueries({ queryKey: serverKey(server.id) });
      await queryClient.invalidateQueries({ queryKey: LIST_KEY, exact: true });
    } catch (cause) {
      setActionError(readableError(cause));
    } finally {
      setBusyId(null);
    }
  };

  const toggle = async (server: McpServerSummary) => {
    if (busyId) return;
    setBusyId(server.id);
    setActionError(null);
    try {
      await setMcpServerEnabled(server.id, !server.enabled);
      await queryClient.invalidateQueries({ queryKey: serverKey(server.id) });
      await queryClient.invalidateQueries({ queryKey: LIST_KEY, exact: true });
      await queryClient.invalidateQueries({ queryKey: STATUS_KEY });
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
      await deleteMcpServer(deleting.id);
      const removedId = deleting.id;
      setDeleting(null);
      if (expandedId === removedId) setExpandedId(null);
      // 服务器没了，它的工具/详情缓存也不该留着。
      await queryClient.invalidateQueries({ queryKey: LIST_KEY });
      await queryClient.invalidateQueries({ queryKey: STATUS_KEY });
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
              <h1 className="text-xl font-semibold tracking-tight text-neutral-900">MCP 服务</h1>
            </div>
            <p className="mt-1 text-xs leading-relaxed text-neutral-500">
              每个服务是一台你自己的 MCP 服务器：填地址和凭据后由网关连接、拉取工具，并同步到检索索引。工具由服务器决定，这里只读展示。
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
              onClick={() => setEditor({ serverId: null, server: null })}
              size="sm"
              type="button"
              variant="primary"
            >
              新建服务
            </Button>
          </div>
        </header>

        <StatusBar
          error={statusQuery.error ? readableError(statusQuery.error) : null}
          loading={statusQuery.isLoading}
          status={statusQuery.data}
        />

        {reindex.data ? (
          <p className="mt-3 text-[11px] text-neutral-500" role="status">
            {`扫描 ${reindex.data.scanned}，变更 ${reindex.data.changed}，写入 ${reindex.data.upserted}，删除 ${reindex.data.deleted}`}
            {reindex.data.failed > 0 ? `，失败 ${reindex.data.failed}` : ""}
            {reindex.data.error ? "；错误：" + reindex.data.error : ""}
          </p>
        ) : null}
        {reindex.error ? (
          <p className={cn(PAGE_ERROR_CLASS, "mt-3")} role="alert">
            {readableError(reindex.error)}
          </p>
        ) : null}

        {actionError ? (
          <p className={cn(PAGE_ERROR_CLASS, "mt-3")} role="alert">
            {actionError}
          </p>
        ) : null}

        {loading ? (
          <div className="mt-4 space-y-3" aria-busy="true" aria-label="加载 MCP 服务列表">
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
            <p className="mt-3 text-sm font-medium text-neutral-700">还没有 MCP 服务</p>
            <p className="mt-1 text-xs text-neutral-500">
              添加一台 MCP 服务器，网关会连接它、拉取工具并同步到检索索引。
            </p>
            <Button
              className="mt-4"
              onClick={() => setEditor({ serverId: null, server: null })}
              size="sm"
              type="button"
              variant="primary"
            >
              新建第一个服务
            </Button>
          </div>
        ) : null}

        {servers.length > 0 ? (
          <ul className="mt-4 space-y-3" aria-label="MCP 服务列表">
            {servers.map((server) => (
              <li key={server.id}>
                <ServerCard
                  busy={busyId === server.id}
                  expanded={expandedId === server.id}
                  onDelete={() => setDeleting(server)}
                  onEdit={() => setEditor({ serverId: server.id, server })}
                  onRefresh={() => void refreshOne(server)}
                  onToggle={() => void toggle(server)}
                  onToggleTools={() =>
                    setExpandedId((current) => (current === server.id ? null : server.id))
                  }
                  server={server}
                />
              </li>
            ))}
          </ul>
        ) : null}
      </div>

      {editor ? (
        <McpServerEditorDialog
          initialServer={editor.server}
          onClose={() => setEditor(null)}
          onSaved={(saved) => {
            setEditor(null);
            // 列表里的 status/tool_count/last_error 都会变，详情也要重新拉。
            void queryClient.invalidateQueries({ queryKey: serverKey(saved.id) });
            void queryClient.invalidateQueries({ queryKey: LIST_KEY, exact: true });
            void queryClient.invalidateQueries({ queryKey: STATUS_KEY });
          }}
          serverId={editor.serverId}
        />
      ) : null}

      {deleting ? (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-neutral-900/40 px-4"
          onClick={() => setDeleting(null)}
          role="presentation"
        >
          <div
            aria-label="删除 MCP 服务"
            aria-modal="true"
            className="w-full max-w-md rounded-xl border border-neutral-200 bg-white p-5 shadow-xl"
            onClick={(event) => event.stopPropagation()}
            role="dialog"
          >
            <h2 className="text-sm font-semibold text-neutral-900">删除 MCP 服务</h2>
            <p className="mt-2 text-xs leading-relaxed text-neutral-600">
              确定删除 <strong className="text-neutral-900">{deleting.name}</strong> 吗？它的工具与索引文档会一起移除；服务器本身不会被改动。
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

interface StatusBarProps {
  error: string | null;
  loading: boolean;
  status: McpServerStatus | undefined;
}

function StatusBar({ error, loading, status }: StatusBarProps) {
  return (
    <section
      aria-label="MCP 服务状态"
      className="mt-4 rounded-lg border border-neutral-200 bg-neutral-50/60 p-4"
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge variant={status?.enabled ? "success" : "neutral"}>
          {status?.enabled ? "MCP 已启用" : "MCP 未启用"}
        </Badge>
        <Badge variant={status && status.servers_error > 0 ? "danger" : "neutral"}>
          {`可达 ${status?.servers_reachable ?? 0}`}
        </Badge>
        <Badge variant="outline">{`工具 ${status?.tools_total ?? 0}`}</Badge>
        <Badge variant="outline">{`已索引 ${status?.indexed_tools ?? 0}`}</Badge>
      </div>
      <p className="mt-2 text-[11px] text-neutral-500">
        {loading
          ? "正在读取状态…"
          : `${status?.servers_total ?? 0} 个服务 · ${status?.servers_active ?? 0} 个运行中 · ${
              status?.servers_pending ?? 0
            } 个待同步 · ${status?.servers_error ?? 0} 个异常 · ${status?.servers_disabled ?? 0} 个已停用`}
      </p>
      <p className="mt-1 text-[11px] text-neutral-500">
        {`${status?.tools_total ?? 0} 个工具，已索引 ${status?.indexed_tools ?? 0} 个`}
        {status?.last_sync_at ? ` · 上次同步 ${status.last_sync_at}` : ""}
      </p>
      {status?.last_error ? (
        <p className="mt-1 text-[11px] text-red-600">{status.last_error}</p>
      ) : null}
      {error ? <p className="mt-1 text-[11px] text-red-600">{error}</p> : null}
    </section>
  );
}

interface ServerCardProps {
  busy: boolean;
  expanded: boolean;
  onDelete: () => void;
  onEdit: () => void;
  onRefresh: () => void;
  onToggle: () => void;
  onToggleTools: () => void;
  server: McpServerSummary;
}

function ServerCard({
  busy,
  expanded,
  onDelete,
  onEdit,
  onRefresh,
  onToggle,
  onToggleTools,
  server,
}: ServerCardProps) {
  return (
    <article
      aria-label={server.name}
      className="rounded-lg border border-neutral-200 bg-white p-4 transition-colors hover:border-neutral-300"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-baseline gap-2">
            <h2 className="truncate text-sm font-semibold text-neutral-900">{server.name}</h2>
            {server.display_name ? (
              <span className="truncate text-xs text-neutral-500">{server.display_name}</span>
            ) : null}
          </div>
          <p className="mt-1 break-all font-mono text-[11px] text-neutral-500">{server.url}</p>
          {server.url_has_query ? (
            <p className="mt-0.5 text-[11px] text-amber-700">
              这个地址带查询串（内容不显示，编辑时留空会沿用原值）
            </p>
          ) : null}
          {server.description ? (
            <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-neutral-600">
              {server.description}
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button
            aria-label={"刷新 " + server.name}
            disabled={busy}
            onClick={onRefresh}
            size="sm"
            type="button"
          >
            刷新
          </Button>
          <Button
            aria-label={"编辑 " + server.name}
            disabled={busy}
            onClick={onEdit}
            size="sm"
            type="button"
            variant="ghost"
          >
            编辑
          </Button>
          <Button
            aria-label={"删除 " + server.name}
            disabled={busy}
            onClick={onDelete}
            size="sm"
            type="button"
            variant="danger"
          >
            删除
          </Button>
          <button
            aria-checked={server.enabled}
            aria-label={server.enabled ? "停用服务" : "启用服务"}
            className={cn(
              "relative h-5 w-9 shrink-0 rounded-full transition-colors",
              server.enabled ? "bg-emerald-500" : "bg-neutral-300",
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
                server.enabled ? "translate-x-4" : "translate-x-0.5",
              )}
            />
          </button>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        <Badge variant={server.enabled ? "success" : "neutral"}>
          {server.enabled ? "已启用" : "已停用"}
        </Badge>
        <Badge title="同步状态" variant={statusVariant(server.status)}>
          {server.status}
        </Badge>
        <Badge variant={server.reachable ? "success" : "danger"}>
          {server.reachable ? "可达" : "不可达"}
        </Badge>
        <Badge variant="outline">{transportLabel(server.transport)}</Badge>
        <Badge variant="outline">{`工具 ${server.tool_count}`}</Badge>
        <Badge variant={server.verify_tls ? "outline" : "warning"}>
          {server.verify_tls ? "校验 TLS" : "不校验 TLS"}
        </Badge>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-neutral-400">
        {server.last_tools_sync_at ? <span>{`工具同步于 ${server.last_tools_sync_at}`}</span> : null}
        {server.last_connected_at ? <span>{`上次连接 ${server.last_connected_at}`}</span> : null}
      </div>

      {server.last_error ? (
        <p className="mt-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-[11px] leading-relaxed text-red-700">
          {server.last_error}
        </p>
      ) : null}

      <div className="mt-3 border-t border-neutral-100 pt-3">
        <Button
          aria-expanded={expanded}
          aria-label={"查看 " + server.name + " 的工具"}
          onClick={onToggleTools}
          size="sm"
          type="button"
          variant="ghost"
        >
          {expanded ? "收起工具" : "查看工具"}
        </Button>
        {expanded ? <ServerTools serverId={server.id} serverName={server.name} /> : null}
      </div>
    </article>
  );
}

interface ServerToolsProps {
  serverId: string;
  serverName: string;
}

/** 只读工具列表：name/call_name/描述/参数/可调用/启用，一个写操作都没有。 */
function ServerTools({ serverId, serverName }: ServerToolsProps) {
  const query = useQuery({
    queryKey: toolsKey(serverId),
    queryFn: () => listMcpServerTools(serverId),
    staleTime: 5_000,
  });
  const tools = query.data?.tools ?? [];

  return (
    <section aria-label={serverName + " 的工具"} className="mt-2">
      {query.isLoading ? (
        <div className="space-y-2" aria-busy="true" aria-label="加载工具列表">
          <Skeleton className="h-9 w-full" />
          <Skeleton className="h-9 w-full" />
        </div>
      ) : null}
      {query.error ? (
        <p className={cn(PAGE_ERROR_CLASS)} role="alert">
          {readableError(query.error)}
        </p>
      ) : null}
      {!query.isLoading && !query.error && tools.length === 0 ? (
        <p className="rounded-md border border-dashed border-neutral-300 px-3 py-3 text-[11px] text-neutral-400">
          这个服务器还没有同步出工具
        </p>
      ) : null}
      {tools.length > 0 ? (
        <ul className="divide-y divide-neutral-200 rounded-md border border-neutral-200">
          {tools.map((tool) => (
            <ToolRow key={tool.call_name || tool.name} tool={tool} />
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function ToolRow({ tool }: { tool: McpServerTool }) {
  return (
    <li className="flex flex-wrap items-start gap-x-3 gap-y-1 px-3 py-2">
      <span className="min-w-0 flex-1">
        <span className="block truncate text-xs font-medium text-neutral-900">
          {tool.display_name ?? tool.name}
        </span>
        <span className="mt-0.5 block truncate font-mono text-[11px] text-neutral-500">
          {tool.call_name}
        </span>
        {tool.description ? (
          <span className="mt-0.5 block text-[11px] leading-relaxed text-neutral-500">
            {tool.description}
          </span>
        ) : null}
      </span>
      <span className="flex shrink-0 flex-wrap items-center gap-1.5">
        <Badge variant={tool.enabled ? "success" : "neutral"}>
          {tool.enabled ? "已启用" : "已停用"}
        </Badge>
        <Badge variant={tool.callable ? "outline" : "warning"}>
          {tool.callable ? "可调用" : "不可调用"}
        </Badge>
        <span className="text-[11px] tabular-nums text-neutral-400">
          {`参数 ${tool.parameters.length}`}
        </span>
      </span>
    </li>
  );
}
