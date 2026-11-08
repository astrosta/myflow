import { apiFetch, apiUrl } from "./base";

/**
 * MCP 服务管理 API 客户端（/api/mcp-servers）。
 *
 * 三条来自网关契约、前端不能自己"补"的规则：
 *
 * 1. **headers 只有掩码**：详情里敏感值已经是 ****，编辑时原样发回，网关负责还原；
 *    前端不做任何"猜真实值"的补全，也不缓存明文。
 * 2. **PUT 是 extra="forbid" 的偏更新**：name/enabled 有各自的生命周期（name 不可改、
 *    enabled 走 /state），显式 null 的 verify_tls 会 422 —— 所以这里只把"确实要改的键"
 *    放进 body，undefined 的一律不出现。
 * 3. **URL 的查询串永远不回传**：列表/详情只给去掉查询串的地址 + url_has_query，
 *    用户不写查询串提交时由网关沿用已保存的那一段。
 */

export type McpTransport = "streamable_http" | "sse";

/** 服务状态是同步结果的派生值，不是可编辑字段。 */
export type McpServerStatusValue = "pending" | "active" | "error" | "disabled" | (string & {});

export interface McpServerSummary {
  id: string;
  name: string;
  display_name: string | null;
  description: string;
  /** 已经去掉查询串的地址。 */
  url: string;
  /** true 表示保存的 URL 还带着一段查询串（内容不回传）。 */
  url_has_query: boolean;
  transport: McpTransport;
  verify_tls: boolean;
  enabled: boolean;
  status: McpServerStatusValue;
  reachable: boolean;
  tool_count: number;
  last_error: string | null;
  last_connected_at: string | null;
  last_tools_sync_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface McpServerDetail extends McpServerSummary {
  /** 敏感值已被网关替换为 ****；原样回传即可。 */
  headers: Record<string, string>;
}

export interface McpServerListPage {
  servers: McpServerSummary[];
  count: number;
}

/** 只读投影：没有 schema、没有 secret，也没有任何可写字段。 */
export interface McpServerTool {
  name: string;
  call_name: string;
  display_name: string | null;
  description: string;
  parameters: string[];
  callable: boolean;
  enabled: boolean;
}

export interface McpServerToolListPage {
  tools: McpServerTool[];
  count: number;
}

export interface McpServerStatus {
  enabled: boolean;
  servers_total: number;
  servers_active: number;
  servers_pending: number;
  servers_error: number;
  servers_disabled: number;
  servers_reachable: number;
  tools_total: number;
  indexed_tools: number;
  last_sync_at: string | null;
  last_error: string | null;
}

export interface McpServerReindexStats {
  scanned: number;
  changed: number;
  upserted: number;
  deleted: number;
  failed: number;
  duration_ms: number;
  error: string | null;
}

export interface McpServerInput {
  name: string;
  display_name?: string | null;
  description?: string;
  url: string;
  transport?: McpTransport;
  /** 编辑已有服务时把网关返回的 **** 原样发回。 */
  headers?: Record<string, string>;
  verify_tls?: boolean;
  enabled?: boolean;
}

/**
 * 编辑用的输入：没有 name/enabled。
 *
 * 类型上先挡一层，运行时再挡一层（见 updateMcpServer）——调用方拿一行 summary
 * 展开拼 body 太容易顺手带上这两个键，而 PUT 会直接 422。
 */
export type McpServerUpdateInput = Partial<Omit<McpServerInput, "name" | "enabled">>;

export class McpServerApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly detail: string;
  readonly traceId: string | null;

  constructor(status: number, code: string | null, detail: string, traceId: string | null) {
    super(detail);
    this.name = "McpServerApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.traceId = traceId;
  }
}

async function readError(response: Response): Promise<McpServerApiError> {
  let code: string | null = null;
  let detail = "HTTP " + response.status;
  let traceId: string | null = null;
  try {
    const body = (await response.json()) as Record<string, unknown>;
    if (typeof body.code === "string") code = body.code;
    if (typeof body.detail === "string") detail = body.detail;
    if (typeof body.trace_id === "string") traceId = body.trace_id;
    // 422 把字段错误放在 errors[]，detail 只是通用文案；把第一个字段错误抬到
    // detail 才能让编辑器把问题指到具体输入框。
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
  return new McpServerApiError(response.status, code, detail, traceId);
}

/** 唯一请求入口：同源前缀、cookie/CSRF 由 apiFetch 负责，错误体转 McpServerApiError。 */
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

const serverPath = (serverId: string) => "/mcp-servers/" + encodeURIComponent(serverId);

function asTransport(value: unknown): McpTransport {
  return value === "sse" ? "sse" : "streamable_http";
}

function asString(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function asNullableString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asNumber(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asBoolean(value: unknown, fallback: boolean): boolean {
  return typeof value === "boolean" ? value : fallback;
}

/**
 * 客户端也切一刀查询串，不只是信 url_has_query。
 *
 * 网关已经保证不回传查询串，但"保证"和"前端状态里真的没有"是两件事：一旦哪天网关
 * 多回了一个带 ?token=... 的地址，它就会原样进入 DOM、日志与崩溃上报。这里按第一个
 * ? 截断（同时丢掉 #fragment），并把"有查询串"这个事实交给 url_has_query。
 */
function splitQuery(raw: string): { url: string; hasQuery: boolean } {
  const index = raw.indexOf("?");
  if (index < 0) return { url: raw, hasQuery: false };
  return { url: raw.slice(0, index), hasQuery: true };
}

type WireServer = Record<string, unknown> & { id?: unknown; name?: unknown; url?: unknown };

function toSummary(raw: WireServer): McpServerSummary {
  const { url, hasQuery } = splitQuery(asString(raw.url, ""));
  return {
    id: asString(raw.id, ""),
    name: asString(raw.name, ""),
    display_name: asNullableString(raw.display_name),
    description: asString(raw.description, ""),
    url,
    url_has_query: asBoolean(raw.url_has_query, false) || hasQuery,
    transport: asTransport(raw.transport),
    verify_tls: asBoolean(raw.verify_tls, true),
    enabled: asBoolean(raw.enabled, true),
    status: asString(raw.status, "pending"),
    reachable: asBoolean(raw.reachable, false),
    tool_count: asNumber(raw.tool_count, 0),
    last_error: asNullableString(raw.last_error),
    last_connected_at: asNullableString(raw.last_connected_at),
    last_tools_sync_at: asNullableString(raw.last_tools_sync_at),
    created_at: asNullableString(raw.created_at),
    updated_at: asNullableString(raw.updated_at),
  };
}

function toDetail(raw: WireServer): McpServerDetail {
  const headers: Record<string, string> = {};
  const rawHeaders = raw.headers;
  if (rawHeaders !== null && typeof rawHeaders === "object" && !Array.isArray(rawHeaders)) {
    for (const [name, value] of Object.entries(rawHeaders)) {
      if (typeof value === "string") headers[name] = value;
    }
  }
  return { ...toSummary(raw), headers };
}

function toTool(raw: Record<string, unknown>): McpServerTool {
  return {
    name: asString(raw.name, ""),
    call_name: asString(raw.call_name, ""),
    display_name: asNullableString(raw.display_name),
    description: asString(raw.description, ""),
    parameters: Array.isArray(raw.parameters)
      ? raw.parameters.filter((part): part is string => typeof part === "string")
      : [],
    callable: asBoolean(raw.callable, false),
    enabled: asBoolean(raw.enabled, true),
  };
}

/** 只把"确实给了值"的键放进 body：undefined 不出现，null 只出现在允许为 null 的字段上。 */
function createBody(input: McpServerInput): Record<string, unknown> {
  const body: Record<string, unknown> = { name: input.name, url: input.url };
  if (input.display_name !== undefined) body.display_name = input.display_name;
  if (input.description !== undefined) body.description = input.description;
  if (input.transport !== undefined) body.transport = input.transport;
  if (input.headers !== undefined) body.headers = input.headers;
  if (input.verify_tls !== undefined) body.verify_tls = input.verify_tls;
  if (input.enabled !== undefined) body.enabled = input.enabled;
  return body;
}

/**
 * PUT body。白名单之外的东西（name/enabled）即使被传进来也不会进请求——
 * 网关的 extra="forbid" 会把它变成一次 422。
 */
function updateBody(input: McpServerUpdateInput): Record<string, unknown> {
  const body: Record<string, unknown> = {};
  if (input.display_name !== undefined) body.display_name = input.display_name;
  if (input.description !== undefined) body.description = input.description;
  if (input.url !== undefined) body.url = input.url;
  if (input.transport !== undefined) body.transport = input.transport;
  if (input.headers !== undefined) body.headers = input.headers;
  if (typeof input.verify_tls === "boolean") body.verify_tls = input.verify_tls;
  return body;
}

export async function listMcpServers(): Promise<McpServerListPage> {
  const body = await request<{ servers?: WireServer[]; count?: number }>("/mcp-servers");
  const servers = (body.servers ?? []).map(toSummary);
  return { servers, count: body.count ?? servers.length };
}

export async function getMcpServer(serverId: string): Promise<McpServerDetail> {
  return toDetail(await request<WireServer>(serverPath(serverId)));
}

export async function createMcpServer(input: McpServerInput): Promise<McpServerDetail> {
  return toDetail(
    await request<WireServer>("/mcp-servers", {
      method: "POST",
      body: JSON.stringify(createBody(input)),
    }),
  );
}

export async function updateMcpServer(
  serverId: string,
  input: McpServerUpdateInput,
): Promise<McpServerDetail> {
  return toDetail(
    await request<WireServer>(serverPath(serverId), {
      method: "PUT",
      body: JSON.stringify(updateBody(input)),
    }),
  );
}

export async function deleteMcpServer(serverId: string): Promise<void> {
  await request<void>(serverPath(serverId), { method: "DELETE" });
}

/** 启停走 /state：PUT 不接受 enabled，否则 422。 */
export async function setMcpServerEnabled(
  serverId: string,
  enabled: boolean,
): Promise<McpServerDetail> {
  return toDetail(
    await request<WireServer>(serverPath(serverId) + "/state", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    }),
  );
}

export async function refreshMcpServer(serverId: string): Promise<McpServerDetail> {
  return toDetail(
    await request<WireServer>(serverPath(serverId) + "/refresh", { method: "POST" }),
  );
}

export async function listMcpServerTools(serverId: string): Promise<McpServerToolListPage> {
  const body = await request<{ tools?: Record<string, unknown>[]; count?: number }>(
    serverPath(serverId) + "/tools",
  );
  const tools = (body.tools ?? []).map(toTool);
  return { tools, count: body.count ?? tools.length };
}

export async function getMcpServerStatus(): Promise<McpServerStatus> {
  const raw = await request<Record<string, unknown>>("/mcp-servers/status");
  return {
    enabled: asBoolean(raw.enabled, true),
    servers_total: asNumber(raw.servers_total, 0),
    servers_active: asNumber(raw.servers_active, 0),
    servers_pending: asNumber(raw.servers_pending, 0),
    servers_error: asNumber(raw.servers_error, 0),
    servers_disabled: asNumber(raw.servers_disabled, 0),
    servers_reachable: asNumber(raw.servers_reachable, 0),
    tools_total: asNumber(raw.tools_total, 0),
    indexed_tools: asNumber(raw.indexed_tools, 0),
    last_sync_at: asNullableString(raw.last_sync_at),
    last_error: asNullableString(raw.last_error),
  };
}

export async function reindexMcpServers(): Promise<McpServerReindexStats> {
  const raw = await request<Record<string, unknown>>("/mcp-servers/reindex", { method: "POST" });
  return {
    scanned: asNumber(raw.scanned, 0),
    changed: asNumber(raw.changed, 0),
    upserted: asNumber(raw.upserted, 0),
    deleted: asNumber(raw.deleted, 0),
    failed: asNumber(raw.failed, 0),
    duration_ms: asNumber(raw.duration_ms, 0),
    error: asNullableString(raw.error),
  };
}
