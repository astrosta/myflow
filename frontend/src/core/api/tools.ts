import { apiFetch, apiUrl } from "./base";

/**
 * ContextForge 工具管理 API 客户端。
 *
 * 浏览器只跟同源的 /api/tools 说话，ContextForge 的 admin token 只存在于网关
 * （M9 的 token 存储约束）。工具文档里的敏感 header 由网关打成 ****，编辑时
 * 把 **** 原样发回，网关负责还原；前端不做任何"猜真实值"的补全。
 */

export type ToolIntegrationType = "REST" | "MCP" | "A2A";

export type ToolRequestType = "SSE" | "GET" | "POST" | "PUT" | "DELETE" | "PATCH";

export type ToolIndexStatus = "indexed" | "stale" | "missing" | "unknown" | (string & {});

export interface ContextForgeTool {
  id: string;
  name: string;
  display_name: string | null;
  description: string;
  url: string | null;
  enabled: boolean;
  reachable: boolean;
  integration_type: ToolIntegrationType;
  request_type: string;
  tags: string[];
  visibility: string | null;
  created_at: string | null;
  updated_at: string | null;
  shadowed_by: string | null;
  index_status: ToolIndexStatus;
}

export interface ContextForgeToolDetail extends ContextForgeTool {
  /** 敏感值已被网关替换为 ****；原样回传即可。 */
  headers: Record<string, string>;
  input_schema: Record<string, unknown>;
  annotations: Record<string, unknown>;
}

export interface ToolListPage {
  tools: ContextForgeTool[];
  count: number;
  next_cursor: string | null;
}

export interface ToolStatus {
  enabled: boolean;
  reachable: boolean;
  auth_ok: boolean;
  /** connected | connecting | reconnecting | disabled | error */
  admin_events: string;
  /** idle | syncing | disabled | error */
  sync: string;
  /** ready | building | unavailable | disabled | error */
  index: string;
  indexed_tools: number;
  total_tools: number;
  last_sync_at: string | null;
  last_sync_reason: string | null;
  last_error: string | null;
}

export interface ToolSearchHit {
  tool_name: string;
  display_name: string | null;
  description: string;
  tags: string[];
  parameters: string[];
  score: number;
}

export interface ToolSearchRetrieval {
  index: string;
  channels: string[];
  candidates: number;
  reranked: boolean;
  warnings: string[];
}

export interface ToolSearchPreview {
  query: string;
  results: ToolSearchHit[];
  retrieval: ToolSearchRetrieval;
}

export interface ToolReindexStats {
  scanned: number;
  changed: number;
  upserted: number;
  deleted: number;
  duration_ms: number;
  error: string | null;
}

export interface ToolWriteInput {
  name: string;
  display_name?: string | null;
  description: string;
  integration_type?: ToolIntegrationType;
  request_type?: ToolRequestType;
  url?: string | null;
  /** 编辑已有工具时把网关返回的 **** 原样发回。 */
  headers?: Record<string, string>;
  input_schema?: Record<string, unknown>;
  annotations?: Record<string, unknown>;
  tags?: string[];
  visibility?: "private" | "team" | "public" | null;
  enabled?: boolean;
}

export interface ToolListParams {
  cursor?: string | null;
  limit?: number;
  q?: string;
  enabled?: boolean;
  integration_type?: string;
}

export class ToolApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly detail: string;
  readonly traceId: string | null;

  constructor(status: number, code: string | null, detail: string, traceId: string | null) {
    super(detail);
    this.name = "ToolApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.traceId = traceId;
  }
}

async function readError(response: Response): Promise<ToolApiError> {
  let code: string | null = null;
  let detail = "HTTP " + response.status;
  let traceId: string | null = null;
  try {
    const body = (await response.json()) as Record<string, unknown>;
    if (typeof body.code === "string") code = body.code;
    if (typeof body.detail === "string") detail = body.detail;
    if (typeof body.trace_id === "string") traceId = body.trace_id;
    // 422 把字段错误放在 errors[]，detail 只是通用文案；把第一个字段错误
    // 抬到 detail 才能让编辑器把问题指到具体输入框。
    if (
      detail === "Request validation failed" &&
      Array.isArray(body.errors) &&
      body.errors.length > 0
    ) {
      const first = body.errors[0] as { loc?: unknown; msg?: unknown };
      const loc = Array.isArray(first.loc)
        ? first.loc.filter((part) => typeof part === "string" && part !== "body")
        : [];
      const field = loc.length > 0 ? loc.join(".") + ": " : "";
      if (typeof first.msg === "string") detail = field + first.msg;
    }
  } catch {
    /* 网关错误页可能不是 JSON */
  }
  return new ToolApiError(response.status, code, detail, traceId);
}

/** 唯一请求入口：同源前缀、cookie/CSRF 由 apiFetch 负责，错误体转 ToolApiError。 */
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await apiFetch(apiUrl(path), { ...init, headers });
  if (!response.ok) throw await readError(response);
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

type WireTool = Partial<ContextForgeTool> & {
  id?: string;
  name?: string;
};

function toTool(raw: WireTool): ContextForgeTool {
  return {
    id: raw.id ?? "",
    name: raw.name ?? "",
    display_name: raw.display_name ?? null,
    description: raw.description ?? "",
    url: raw.url ?? null,
    enabled: raw.enabled ?? true,
    reachable: raw.reachable ?? true,
    integration_type: raw.integration_type ?? "REST",
    request_type: raw.request_type ?? "SSE",
    tags: raw.tags ?? [],
    visibility: raw.visibility ?? null,
    created_at: raw.created_at ?? null,
    updated_at: raw.updated_at ?? null,
    shadowed_by: raw.shadowed_by ?? null,
    index_status: raw.index_status ?? "unknown",
  };
}

function toDetail(raw: WireTool & Partial<ContextForgeToolDetail>): ContextForgeToolDetail {
  return {
    ...toTool(raw),
    headers: raw.headers ?? {},
    input_schema: raw.input_schema ?? {},
    annotations: raw.annotations ?? {},
  };
}

const toolPath = (toolId: string) => "/tools/" + encodeURIComponent(toolId);

export async function listTools(params: ToolListParams = {}): Promise<ToolListPage> {
  const search = new URLSearchParams();
  if (params.cursor) search.set("cursor", params.cursor);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  if (params.q && params.q.trim()) search.set("q", params.q.trim());
  if (params.enabled !== undefined) search.set("enabled", String(params.enabled));
  if (params.integration_type) search.set("integration_type", params.integration_type);
  const query = search.toString();
  const body = await request<{ tools?: WireTool[]; count?: number; next_cursor?: string | null }>(
    "/tools" + (query ? "?" + query : ""),
  );
  const tools = (body.tools ?? []).map(toTool);
  return {
    tools,
    count: body.count ?? tools.length,
    next_cursor: body.next_cursor ?? null,
  };
}

export async function getTool(toolId: string): Promise<ContextForgeToolDetail> {
  return toDetail(await request<WireTool & Partial<ContextForgeToolDetail>>(toolPath(toolId)));
}

export async function createTool(input: ToolWriteInput): Promise<ContextForgeToolDetail> {
  return toDetail(
    await request<WireTool & Partial<ContextForgeToolDetail>>("/tools", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  );
}

export async function updateTool(
  toolId: string,
  input: ToolWriteInput,
): Promise<ContextForgeToolDetail> {
  return toDetail(
    await request<WireTool & Partial<ContextForgeToolDetail>>(toolPath(toolId), {
      method: "PUT",
      body: JSON.stringify(input),
    }),
  );
}

export async function deleteTool(toolId: string): Promise<void> {
  await request<void>(toolPath(toolId), { method: "DELETE" });
}

export async function setToolEnabled(
  toolId: string,
  enabled: boolean,
): Promise<ContextForgeToolDetail> {
  return toDetail(
    await request<WireTool & Partial<ContextForgeToolDetail>>(toolPath(toolId) + "/state", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  );
}

export async function getToolsStatus(): Promise<ToolStatus> {
  const raw = await request<Partial<ToolStatus>>("/tools/status");
  return {
    enabled: raw.enabled ?? false,
    reachable: raw.reachable ?? false,
    auth_ok: raw.auth_ok ?? false,
    admin_events: raw.admin_events ?? "disabled",
    sync: raw.sync ?? "disabled",
    index: raw.index ?? "disabled",
    indexed_tools: raw.indexed_tools ?? 0,
    total_tools: raw.total_tools ?? 0,
    last_sync_at: raw.last_sync_at ?? null,
    last_sync_reason: raw.last_sync_reason ?? null,
    last_error: raw.last_error ?? null,
  };
}

export async function reindexTools(): Promise<ToolReindexStats> {
  const raw = await request<Partial<ToolReindexStats>>("/tools/reindex", { method: "POST" });
  return {
    scanned: raw.scanned ?? 0,
    changed: raw.changed ?? 0,
    upserted: raw.upserted ?? 0,
    deleted: raw.deleted ?? 0,
    duration_ms: raw.duration_ms ?? 0,
    error: raw.error ?? null,
  };
}

export async function previewToolSearch(q: string, limit = 5): Promise<ToolSearchPreview> {
  const search = new URLSearchParams({ q, limit: String(limit) });
  const raw = await request<{
    query?: string;
    results?: Partial<ToolSearchHit>[];
    retrieval?: Partial<ToolSearchRetrieval>;
  }>("/tools/search?" + search.toString());
  return {
    query: raw.query ?? q,
    results: (raw.results ?? []).map((hit) => ({
      tool_name: hit.tool_name ?? "",
      display_name: hit.display_name ?? null,
      description: hit.description ?? "",
      tags: hit.tags ?? [],
      parameters: hit.parameters ?? [],
      score: hit.score ?? 0,
    })),
    retrieval: {
      index: raw.retrieval?.index ?? "unavailable",
      channels: raw.retrieval?.channels ?? [],
      candidates: raw.retrieval?.candidates ?? 0,
      reranked: raw.retrieval?.reranked ?? false,
      warnings: raw.retrieval?.warnings ?? [],
    },
  };
}
