import { apiFetch, apiUrl } from "./base";

/**
 * Git 技能目录 / 私有草稿 / 审核 API。
 *
 * 路径、方法、参数、响应形状以网关 router 为准（skill_drafts / skill_reviews /
 * skills），不要在页面里自己拼 URL。错误体沿用网关的 { code, detail, trace_id }，
 * 页面按 code 决定提示，detail 只作为人读的补充。
 *
 * 两个和后端模型对齐时要注意的地方：
 * - 作者路由（/skills/mine、POST /skills）返回的行主键叫 `id`，计划里的
 *   前端类型叫 `skill_id`；客户端在边界上统一成 `skill_id`，并保留 `id`。
 * - GET /skills 与 PUT /skills/{id}/preference 返回的是目录行（`skill_id`），
 *   在 filesystem 来源下 `skill_id` 可能为空，这里回退到 id/name。
 * - 内置行也走 GET /skills，scope/status 是 `builtin`、skill_id 可能为 null；
 *   客户端保留 scope，只把主键回退到 id/name。
 */

export type SkillScope = "public" | "private" | "builtin";

export type SkillStatus =
  | "draft"
  | "pending"
  | "approved"
  | "rejected"
  | "withdrawn"
  | "builtin";

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
  enabled: boolean;
  global_enabled?: boolean | null;
  container_file?: string | null;
  /** 作者自己的最近一次审核结论；只有 /skills/mine 与草稿详情会带。 */
  reject_reason?: string | null;
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
  /** 网关还会回这两个字段；计划里的类型没列，前端按可选读。 */
  draft_ref?: string | null;
  skill_path?: string | null;
}

export interface SkillSubmission {
  submission_id: string;
  skill_id: string;
  version_id: string;
  status: string;
  submitted_at: string;
  owner_user_id: string;
  reject_reason?: string | null;
}

export interface ReviewSummary {
  submission_id: string;
  skill_id: string;
  name: string;
  version: string;
  owner_user_id: string;
  submitted_at: string;
  submitted_commit: string;
}

export interface ReviewDetail extends ReviewSummary {
  files: DraftFile[];
  skill_md: string;
  diff: string;
  metadata: Record<string, unknown>;
  package_bytes: number;
}

export interface SkillRelease {
  release_id: string;
  skill_id: string;
  skill_version: string;
  git_repository: string;
  git_commit: string;
  skill_path: string;
  public_name: string;
  source_commit?: string | null;
  approved_by: string;
  created_at: string;
}

export interface DraftView {
  skill: SkillListItem;
  files: DraftFile[];
  pending: boolean;
}

export interface UnpublishResult {
  skill_id: string;
  unpublished: boolean;
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
 * SkillApiError。DELETE 与 activate 是 204，没有 JSON body。
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
  scope?: SkillScope;
  status?: string | null;
  enabled?: boolean;
};

/**
 * 作者路由的行主键是 `id`，目录路由的是 `skill_id`；这里统一成 `skill_id`。
 * 缺字段就补安全默认值，页面不必到处判空。
 */
function toSkillListItem(raw: WireSkill): SkillListItem {
  const skillId = raw.skill_id ?? raw.id ?? raw.name ?? "";
  const status = raw.status ?? "draft";
  return {
    ...raw,
    skill_id: skillId,
    id: raw.id ?? skillId,
    name: raw.name ?? "",
    description: raw.description ?? "",
    scope: raw.scope ?? "private",
    owner_user_id: raw.owner_user_id ?? null,
    skill_version: raw.skill_version ?? null,
    version_id: raw.version_id ?? null,
    git_commit: raw.git_commit ?? null,
    skill_path: raw.skill_path ?? null,
    status: status as SkillStatus,
    enabled: raw.enabled ?? true,
    global_enabled: raw.global_enabled ?? null,
    container_file: raw.container_file ?? null,
  };
}

const skillPath = (skillId: string) => "/skills/" + encodeURIComponent(skillId);
const reviewPath = (submissionId: string) =>
  "/admin/skills/reviews/" + encodeURIComponent(submissionId);

export async function listSkills(
  scope: "effective" | "global" = "effective",
): Promise<SkillListItem[]> {
  const query = scope === "global" ? "?scope=global" : "";
  const body = await request<{ skills: WireSkill[] }>("/skills" + query);
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
  const body = await request<{ skill: WireSkill; files: DraftFile[]; pending: boolean }>(
    skillPath(skillId) + "/draft",
  );
  return { skill: toSkillListItem(body.skill), files: body.files, pending: body.pending };
}

/** 草稿文件树与 openDraft 同形，只是不触发"从公共版本复制"的副作用。 */
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

export async function uploadDraftFiles(
  skillId: string,
  files: readonly File[],
): Promise<DraftFile[]> {
  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);
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

export function submitSkill(skillId: string): Promise<SkillSubmission> {
  return request<SkillSubmission>(skillPath(skillId) + "/submit", { method: "POST" });
}

export function withdrawSkill(skillId: string): Promise<SkillSubmission> {
  return request<SkillSubmission>(skillPath(skillId) + "/withdraw", { method: "POST" });
}

export async function deleteSkill(skillId: string): Promise<void> {
  await request<void>(skillPath(skillId), { method: "DELETE" });
}

export async function listReviews(): Promise<ReviewSummary[]> {
  const body = await request<{ items: ReviewSummary[] }>("/admin/skills/reviews");
  return body.items;
}

export function getReview(submissionId: string): Promise<ReviewDetail> {
  return request<ReviewDetail>(reviewPath(submissionId));
}

export function approveReview(submissionId: string): Promise<SkillRelease> {
  return request<SkillRelease>(reviewPath(submissionId) + "/approve", { method: "POST" });
}

export function rejectReview(submissionId: string, reason: string): Promise<SkillSubmission> {
  return request<SkillSubmission>(reviewPath(submissionId) + "/reject", {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

export function unpublishSkill(skillId: string): Promise<UnpublishResult> {
  return request<UnpublishResult>("/admin/skills/" + encodeURIComponent(skillId) + "/unpublish", {
    method: "POST",
  });
}

export async function activateRelease(skillId: string, releaseId: string): Promise<void> {
  await request<void>("/admin/skills/" + encodeURIComponent(skillId) + "/activate", {
    method: "POST",
    body: JSON.stringify({ release_id: releaseId }),
  });
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
