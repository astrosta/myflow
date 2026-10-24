import { apiFetch, apiUrl } from "./base";

/**
 * 知识库 API。
 *
 * 所有请求走同源 /api；上传交给浏览器生成 multipart boundary，不要手写
 * Content-Type。错误体沿用网关的 { code, detail, trace_id }，页面按 code
 * 决定提示，detail 只作为人读的补充。
 */

export type KnowledgeBaseType =
  | "technical"
  | "operations"
  | "faq"
  | "security"
  | "api"
  | "legal";

export interface KnowledgeTypeMeta {
  value: KnowledgeBaseType;
  label: string;
  description: string;
}

export const KNOWLEDGE_TYPES: readonly KnowledgeTypeMeta[] = [
  { value: "technical", label: "技术文档", description: "架构、安装、配置与使用说明；按标题层级切分。" },
  { value: "operations", label: "运维手册", description: "部署、巡检、排障与回滚；按操作步骤和命令块切分。" },
  { value: "faq", label: "FAQ 类文档", description: "常见问题与回答；一问一答为一个分片。" },
  { value: "security", label: "安全合规文档", description: "安全要求、合规条款与审计证据；按条款切分。" },
  { value: "api", label: "API 参考文档", description: "接口、参数、返回值和示例；按接口/符号切分。" },
  {
    value: "legal",
    label: "法律法规",
    description: "法律、法规与规章条文；按“第X条”切分，条号写入章节。",
  },
];

export interface KnowledgeTypeStatus {
  value: KnowledgeBaseType;
  strategy: string;
  chunk_size_tokens: number;
  chunk_overlap_tokens: number;
}

export interface KnowledgeRetrievalStatus {
  mode: "hybrid" | "vector";
  keyword_enabled: boolean;
  keyword_analyzer: string;
  rrf_k: number;
  rerank_enabled: boolean;
  rerank_model: string;
}

export interface KnowledgeStatus {
  enabled: boolean;
  parser: string;
  parser_mode: string;
  embedding_model: string;
  embedding_dimensions: number;
  vector_store: string;
  collection: string;
  allowed_extensions: string[];
  text_extensions: string[];
  max_file_size_bytes: number;
  max_total_size_bytes: number;
  max_files_per_request: number;
  max_documents_per_base: number;
  retrieval?: KnowledgeRetrievalStatus;
  types: KnowledgeTypeStatus[];
}

export interface KnowledgeBase {
  id: string;
  name: string;
  type: KnowledgeBaseType;
  description: string | null;
  document_count: number;
  chunk_count: number;
  created_at: string;
  updated_at: string;
}

export type KnowledgeDocumentStatus =
  | "queued"
  | "parsing"
  | "chunking"
  | "embedding"
  | "ready"
  | "failed";

export interface KnowledgeDocument {
  id: string;
  knowledge_base_id: string;
  filename: string;
  mime_type: string;
  size_bytes: number;
  status: KnowledgeDocumentStatus;
  chunk_count: number;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeSearchHit {
  chunk_id: string;
  document_id: string;
  filename: string;
  section: string;
  page: number;
  chunk_index: number;
  score: number;
  knowledge_base_id: string;
  knowledge_base_name: string;
  knowledge_base_type: KnowledgeBaseType;
  content: string;
  rrf_score: number | null;
  rerank_score: number | null;
}

export type KnowledgeRetrievalMode = "vector" | "hybrid";

export interface KnowledgeSearchResult {
  query: string;
  keyword_query: string;
  retrieval_mode: KnowledgeRetrievalMode;
  reranked: boolean;
  results: KnowledgeSearchHit[];
  truncated: boolean;
}

export class KnowledgeApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly traceId: string | null;

  constructor(status: number, code: string | null, detail: string, traceId: string | null) {
    super(detail);
    this.name = "KnowledgeApiError";
    this.status = status;
    this.code = code;
    this.traceId = traceId;
  }
}

async function readError(response: Response): Promise<KnowledgeApiError> {
  let code: string | null = null;
  let detail = "HTTP " + response.status;
  let traceId: string | null = null;
  try {
    const body = (await response.json()) as Record<string, unknown>;
    if (typeof body.code === "string") code = body.code;
    if (typeof body.detail === "string") detail = body.detail;
    if (typeof body.trace_id === "string") traceId = body.trace_id;
  } catch {
    /* 网关错误页可能不是 JSON */
  }
  return new KnowledgeApiError(response.status, code, detail, traceId);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const response = await apiFetch(apiUrl(path), { ...init, headers });
  if (!response.ok) throw await readError(response);
  return (await response.json()) as T;
}

export function getKnowledgeStatus(): Promise<KnowledgeStatus> {
  return request<KnowledgeStatus>("/knowledge/status");
}

export function listKnowledgeBases(): Promise<{ bases: KnowledgeBase[]; count: number }> {
  return request<{ bases: KnowledgeBase[]; count: number }>("/knowledge/bases");
}

export function createKnowledgeBase(input: {
  name: string;
  type: KnowledgeBaseType;
  description?: string | null;
}): Promise<KnowledgeBase> {
  return request<KnowledgeBase>("/knowledge/bases", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function getKnowledgeBase(baseId: string): Promise<KnowledgeBase> {
  return request<KnowledgeBase>("/knowledge/bases/" + encodeURIComponent(baseId));
}

export function deleteKnowledgeBase(baseId: string): Promise<{ deleted: string }> {
  return request<{ deleted: string }>("/knowledge/bases/" + encodeURIComponent(baseId), {
    method: "DELETE",
  });
}

export function listKnowledgeDocuments(
  baseId: string,
): Promise<{ documents: KnowledgeDocument[]; count: number }> {
  return request<{ documents: KnowledgeDocument[]; count: number }>(
    "/knowledge/bases/" + encodeURIComponent(baseId) + "/documents",
  );
}

export async function uploadKnowledgeFiles(
  baseId: string,
  files: readonly File[],
): Promise<{ documents: KnowledgeDocument[]; count: number }> {
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);
  return request<{ documents: KnowledgeDocument[]; count: number }>(
    "/knowledge/bases/" + encodeURIComponent(baseId) + "/documents",
    { method: "POST", body: form },
  );
}

export function deleteKnowledgeDocument(
  baseId: string,
  documentId: string,
): Promise<{ deleted: string }> {
  return request<{ deleted: string }>(
    "/knowledge/bases/" +
      encodeURIComponent(baseId) +
      "/documents/" +
      encodeURIComponent(documentId),
    { method: "DELETE" },
  );
}

export function reindexKnowledgeDocument(
  baseId: string,
  documentId: string,
): Promise<KnowledgeDocument> {
  return request<KnowledgeDocument>(
    "/knowledge/bases/" +
      encodeURIComponent(baseId) +
      "/documents/" +
      encodeURIComponent(documentId) +
      "/reindex",
    { method: "POST" },
  );
}

export interface SearchKnowledgeOptions {
  query: string;
  keyword?: string | null;
  baseId?: string | null;
  type?: KnowledgeBaseType | null;
  topK?: number;
  signal?: AbortSignal;
}

export function searchKnowledge(options: SearchKnowledgeOptions): Promise<KnowledgeSearchResult> {
  const params = new URLSearchParams({ q: options.query });
  if (options.keyword?.trim()) params.set("keyword", options.keyword.trim());
  if (options.baseId) params.set("base_id", options.baseId);
  if (options.type) params.set("type", options.type);
  if (options.topK) params.set("top_k", String(options.topK));
  return request<KnowledgeSearchResult>("/knowledge/search?" + params.toString(), {
    signal: options.signal,
  });
}

export const KNOWLEDGE_ACTIVE_STATUSES: ReadonlySet<KnowledgeDocumentStatus> = new Set([
  "queued",
  "parsing",
  "chunking",
  "embedding",
]);

export function knowledgeRetrievalModeLabel(mode: string): string {
  return mode === "hybrid" ? "混合检索" : "向量检索";
}

export function knowledgeTypeLabel(type: string): string {
  return KNOWLEDGE_TYPES.find((item) => item.value === type)?.label ?? type;
}

export function knowledgeDocumentStatusLabel(status: string): string {
  switch (status) {
    case "queued":
      return "等待处理";
    case "parsing":
      return "解析中";
    case "chunking":
      return "分片中";
    case "embedding":
      return "向量化中";
    case "ready":
      return "已就绪";
    case "failed":
      return "失败";
    default:
      return status;
  }
}
