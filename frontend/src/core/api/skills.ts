import { apiFetch, apiUrl } from "./base";

/**
 * 用户隔离的 Git 技能 API：草稿 / 保存 / 安全扫描 / 生效。
 *
 * 路径、方法、参数、响应形状以网关 router 为准。错误体沿用网关的
 * { code, detail, trace_id }，页面按 code 决定提示。
 *
 * 没有审核、发布、公共目录或管理员接口：每个技能只属于当前用户；保存只产生
 * 草稿版本，安全扫描通过并点击生效后才进入运行时与向量索引。
 */

export type SkillScope = "private" | "builtin";

export type SkillSource = "manual" | "extracted";

export type SkillStatus =
  | "draft"
  | "unscanned"
  | "scan_failed"
  | "scanned"
  | "active"
  | "builtin";

export interface SecurityFinding {
  rule_id: string;
  title: string;
  severity: string;
  category: string;
  file_path: string | null;
  line_number: number | null;
  remediation: string | null;
}

export interface SecurityScanSummary {
  scan_id: string;
  version_id: string;
  status: "completed" | "failed";
  is_safe: boolean;
  max_severity: string;
  findings_count: number;
  scanner_version: string | null;
  duration_ms: number;
  created_at: string;
}

export interface SecurityScan extends SecurityScanSummary {
  skill_id: string;
  findings: SecurityFinding[];
  scanner: string;
  error: string | null;
}

export interface SkillListItem {
  skill_id: string;
  /** 作者路由的 wire 主键；与 skill_id 同值，保留以便和网关逐个字段对照。 */
  id?: string;
  name: string;
  description: string;
  scope: SkillScope;
  owner_user_id?: string | null;
  skill_version: string | null;
  version_id: string | null;
  git_commit: string | null;
  skill_path: string | null;
  status: SkillStatus;
  source?: SkillSource;
  source_uri?: string | null;
  enabled: boolean;
  container_file?: string | null;
  index_status?: string | null;
  active_version_id?: string | null;
  active?: boolean;
  latest_scan?: SecurityScanSummary | null;
  can_activate?: boolean;
  activation_blocked_reason?: string | null;
}

export interface DraftFile {
  path: string;
  size_bytes: number;
  mode: string;
  sha256: string;
}

export interface DraftFileContent extends DraftFile {
  content: string;
  encoding: "utf-8" | "base64";
}

export interface SkillVersion {
  version_id: string;
  skill_version: string;
  package_sha256: string;
  skill_md_sha256: string;
  draft_commit: string | null;
  index_status: string;
  created_at: string;
  draft_ref?: string | null;
  skill_path?: string | null;
}

export interface DraftView {
  skill: SkillListItem;
  files: DraftFile[];
  latest_version: SkillVersion | null;
  latest_scan: SecurityScan | null;
}

export interface ActivateResult {
  version: SkillVersion;
  active_version_id: string;
  active: boolean;
  index_status: string;
  indexed: boolean | null;
}

export class SkillApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly detail: string;
  readonly traceId: string | null;

  constructor(status: number, code: string | null, detail: string, traceId: string | null) {
    super(detail);
    this.name = "SkillApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
    this.traceId = traceId;
  }
}

async function readError(response: Response): Promise<SkillApiError> {
  let code: string | null = null;
  let detail = "HTTP " + response.status;
  let traceId: string | null = null;
  try {
    const body = (await response.json()) as Record<string, unknown>;
    if (typeof body.code === "string") code = body.code;
    if (typeof body.detail === "string") detail = body.detail;
    if (typeof body.trace_id === "string") traceId = body.trace_id;
    // 参数校验的 422 把字段错误放在 errors[]，detail 只是通用文案；
    // 丢掉 errors[] 会让用户只看到 "Request validation failed"。
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
  return new SkillApiError(response.status, code, detail, traceId);
}

/**
 * 唯一的请求入口：拼同源前缀、按 body 类型补 Content-Type、把错误体转成
 * SkillApiError。DELETE 与 204 没有 JSON body。
 */
async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined && !(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  const response = await apiFetch(apiUrl(path), { ...init, headers });
  if (!response.ok) throw await readError(response);
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

type WireSkill = Partial<SkillListItem> & {
  id?: string;
  skill_id?: string | null;
  name?: string;
  description?: string | null;
  category?: "custom" | "builtin";
  scope?: SkillScope;
  status?: string | null;
  enabled?: boolean;
};

/**
 * 作者路由的行主键是 id，目录路由的是 skill_id；这里统一成 skill_id。
 * 缺字段就补安全默认值，页面不必到处判空。
 */
function toSkillListItem(raw: WireSkill): SkillListItem {
  const skillId = raw.skill_id ?? raw.id ?? raw.name ?? "";
  const status = raw.status ?? "draft";
  const scope: SkillScope =
    raw.scope ??
    (raw.category === "builtin" ? "builtin" : "private");
  return {
    ...raw,
    skill_id: skillId,
    id: raw.id ?? skillId,
    name: raw.name ?? "",
    description: raw.description ?? "",
    scope,
    owner_user_id: raw.owner_user_id ?? null,
    skill_version: raw.skill_version ?? null,
    version_id: raw.version_id ?? null,
    git_commit: raw.git_commit ?? null,
    skill_path: raw.skill_path ?? null,
    status: status as SkillStatus,
    source: raw.source ?? "manual",
    source_uri: raw.source_uri ?? null,
    enabled: raw.enabled ?? false,
    container_file: raw.container_file ?? null,
    index_status: raw.index_status ?? null,
    active_version_id: raw.active_version_id ?? null,
    active: raw.active ?? false,
    latest_scan: raw.latest_scan ?? null,
    can_activate: raw.can_activate ?? false,
    activation_blocked_reason: raw.activation_blocked_reason ?? null,
  };
}

const skillPath = (skillId: string) => "/skills/" + encodeURIComponent(skillId);

export async function listSkills(): Promise<SkillListItem[]> {
  const body = await request<{ skills: WireSkill[] }>("/skills");
  return body.skills.map(toSkillListItem);
}

export async function listMySkills(): Promise<SkillListItem[]> {
  const body = await request<{ skills: WireSkill[] }>("/skills/mine");
  return body.skills.map(toSkillListItem);
}

export async function createSkill(input: {
  name: string;
  description: string;
}): Promise<SkillListItem> {
  return toSkillListItem(
    await request<WireSkill>("/skills", {
      method: "POST",
      body: JSON.stringify(input),
    }),
  );
}

export async function openDraft(skillId: string): Promise<DraftFile[]> {
  const body = await request<{ files: DraftFile[] }>(skillPath(skillId) + "/draft/open", {
    method: "POST",
  });
  return body.files;
}

export async function getDraft(skillId: string): Promise<DraftView> {
  const body = await request<{
    skill: WireSkill;
    files: DraftFile[];
    latest_version: SkillVersion | null;
    latest_scan: SecurityScan | null;
  }>(skillPath(skillId) + "/draft");
  return {
    skill: toSkillListItem(body.skill),
    files: body.files,
    latest_version: body.latest_version ?? null,
    latest_scan: body.latest_scan ?? null,
  };
}

export async function listDraftFiles(skillId: string): Promise<DraftFile[]> {
  const body = await request<{ files: DraftFile[] }>(skillPath(skillId) + "/draft/tree");
  return body.files;
}

export function readDraftFile(skillId: string, path: string): Promise<DraftFileContent> {
  return request<DraftFileContent>(
    skillPath(skillId) + "/draft/file?path=" + encodeURIComponent(path),
  );
}

export function writeDraftFile(
  skillId: string,
  input: { path: string; content: string; mode?: "text" | "executable" },
): Promise<DraftFile> {
  return request<DraftFile>(skillPath(skillId) + "/draft/file", {
    method: "PUT",
    body: JSON.stringify({
      path: input.path,
      content: input.content,
      mode: input.mode ?? "text",
    }),
  });
}

/**
 * 文件夹上传时 file.name 只剩文件名，webkitRelativePath 才带着目录层级。
 * 去掉用户选中的最外层目录，让所选目录的内容落到技能包根目录（SKILL.md
 * 必须在根目录）；普通多选上传没有相对路径，继续使用 file.name。
 */
function uploadPath(file: File): string {
  const relative = file.webkitRelativePath;
  if (!relative) return file.name;
  const separator = relative.indexOf("/");
  const path = separator >= 0 ? relative.slice(separator + 1) : relative;
  return path || file.name;
}

export async function uploadDraftFiles(
  skillId: string,
  files: readonly File[],
): Promise<DraftFile[]> {
  const form = new FormData();
  for (const file of files) form.append("files", file, uploadPath(file));
  // 不要手动设 Content-Type：multipart boundary 由浏览器生成。
  const body = await request<{ files: DraftFile[] }>(skillPath(skillId) + "/draft/files", {
    method: "POST",
    body: form,
  });
  return body.files;
}

export async function deleteDraftFile(skillId: string, path: string): Promise<void> {
  await request<void>(skillPath(skillId) + "/draft/file?path=" + encodeURIComponent(path), {
    method: "DELETE",
  });
}

export async function saveDraft(skillId: string): Promise<SkillVersion> {
  const body = await request<{ version: SkillVersion }>(skillPath(skillId) + "/draft/save", {
    method: "POST",
  });
  return body.version;
}

function parseSseBlock(block: string): { event: string; data: unknown } | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const rawLine of block.split(/\r?\n/)) {
    const line = rawLine.trimEnd();
    if (!line || line.startsWith(":")) continue;
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (dataLines.length === 0) return null;
  return { event, data: JSON.parse(dataLines.join("\n")) as unknown };
}

/**
 * 扫描走 SSE：阶段状态先到，最后一帧才是和原来 JSON 一样的扫描结果。
 * 旧网关若仍回 JSON，按 JSON 读，避免前端和网关必须同一时刻发布。
 */
export async function scanSkill(
  skillId: string,
  onStatus?: (message: string) => void,
): Promise<SecurityScan> {
  const headers = new Headers({ Accept: "text/event-stream" });
  const response = await apiFetch(apiUrl(skillPath(skillId) + "/scan"), {
    method: "POST",
    headers,
  });
  if (!response.ok) throw await readError(response);
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("text/event-stream")) {
    const text = await response.text();
    return JSON.parse(text) as SecurityScan;
  }
  if (!response.body) {
    throw new SkillApiError(response.status, "invalid_response", "扫描没有返回内容", null);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let result: SecurityScan | null = null;

  const take = (block: string) => {
    const parsed = parseSseBlock(block);
    if (!parsed) return;
    if (parsed.event === "status") {
      const message = (parsed.data as { message?: unknown }).message;
      if (typeof message === "string" && message) onStatus?.(message);
      return;
    }
    if (parsed.event === "result") {
      result = parsed.data as SecurityScan;
      return;
    }
    if (parsed.event === "error") {
      const body = parsed.data as { status?: unknown; code?: unknown; detail?: unknown };
      throw new SkillApiError(
        typeof body.status === "number" ? body.status : 500,
        typeof body.code === "string" ? body.code : "internal_error",
        typeof body.detail === "string" ? body.detail : "扫描失败",
        null,
      );
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split(/\r?\n\r?\n/);
    buffer = blocks.pop() ?? "";
    for (const block of blocks) take(block);
  }
  if (buffer.trim()) take(buffer);
  if (!result) {
    throw new SkillApiError(response.status, "invalid_response", "扫描没有返回结果", null);
  }
  return result;
}

export async function listScans(skillId: string, limit = 20): Promise<SecurityScan[]> {
  const body = await request<{ scans: SecurityScan[] }>(
    skillPath(skillId) + "/scans?limit=" + encodeURIComponent(String(limit)),
  );
  return body.scans;
}

export function activateSkill(
  skillId: string,
  versionId?: string | null,
): Promise<ActivateResult> {
  return request<ActivateResult>(skillPath(skillId) + "/activate", {
    method: "POST",
    body: JSON.stringify(versionId ? { version_id: versionId } : {}),
  });
}

export async function deleteSkill(skillId: string): Promise<void> {
  await request<void>(skillPath(skillId), { method: "DELETE" });
}

export async function setSkillPreference(
  skillId: string,
  enabled: boolean,
): Promise<SkillListItem> {
  return toSkillListItem(
    await request<WireSkill>(skillPath(skillId) + "/preference", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  );
}
