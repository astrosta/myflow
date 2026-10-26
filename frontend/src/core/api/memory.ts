import { apiFetch, apiUrl } from "./base";

export interface ProfileFieldView {
  value: unknown;
  source: string;
  evidence_tier: string | null;
  confidence: number;
  observed_at: string;
}

export interface ProfileView {
  schema_version: number;
  revision: number;
  fields: Record<string, ProfileFieldView>;
}

export interface PendingItem {
  id: string;
  content: string;
  confidence: number;
  evidence_tier: string | null;
  evidence_quote: string | null;
  source: string;
  created_at: string;
  updated_at: string;
  revision: number;
}

export interface PendingPage {
  items: PendingItem[];
  next_cursor: string | null;
}

export interface SkillCandidate {
  id: string;
  status: string;
  revision: number;
  private_name: string;
  description: string | null;
  trigger_text: string;
  package_sha256: string;
  source_episode_ids: string[];
  created_at: string;
}

export interface ConfirmCandidateInput {
  expected_revision: number;
  package_sha256: string;
  idempotency_key: string;
}

export class MemoryApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly detail: string;

  constructor(status: number, code: string | null, detail: string) {
    super(detail);
    this.name = "MemoryApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

async function readError(response: Response): Promise<MemoryApiError> {
  let code: string | null = null;
  let detail = "HTTP " + response.status;
  try {
    const body = (await response.json()) as Record<string, unknown>;
    if (typeof body.code === "string") code = body.code;
    if (typeof body.detail === "string") detail = body.detail;
  } catch {
    /* gateway may return a non-JSON error page */
  }
  return new MemoryApiError(response.status, code, detail);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await apiFetch(apiUrl(path), { ...init, headers });
  if (!response.ok) throw await readError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function getProfile(): Promise<ProfileView> {
  return request<ProfileView>("/memory/profile");
}

export function patchProfile(
  fields: Record<string, unknown>,
  expectedRevision: number,
): Promise<ProfileView> {
  return request<ProfileView>("/memory/profile", {
    method: "PATCH",
    body: JSON.stringify({ fields, expected_revision: expectedRevision }),
  });
}

export function clearProfileField(fieldPath: string): Promise<{ cleared: string }> {
  return request<{ cleared: string }>(
    "/memory/profile/" + encodeURIComponent(fieldPath),
    { method: "DELETE" },
  );
}

export function listPending(): Promise<PendingPage> {
  return request<PendingPage>("/memory/pending");
}

export function promoteSemantic(id: string, expectedRevision: number): Promise<unknown> {
  return request("/memory/items/semantic/" + encodeURIComponent(id) + "/promote", {
    method: "POST",
    body: JSON.stringify({ expected_revision: expectedRevision }),
  });
}

export function deleteMemoryItem(kind: string, id: string): Promise<{ deleted: string }> {
  return request<{ deleted: string }>(
    "/memory/items/" + encodeURIComponent(kind) + "/" + encodeURIComponent(id),
    { method: "DELETE" },
  );
}

export function listSkillCandidates(): Promise<SkillCandidate[]> {
  return request<SkillCandidate[]>("/memory/skill-candidates");
}

export function confirmSkillCandidate(
  id: string,
  input: ConfirmCandidateInput,
): Promise<{ candidate_id: string; skill_id: string; version_id: string }> {
  return request("/memory/skill-candidates/" + encodeURIComponent(id) + "/confirm", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

export function rejectSkillCandidate(
  id: string,
  expectedRevision: number,
  reason: string,
): Promise<SkillCandidate> {
  return request<SkillCandidate>(
    "/memory/skill-candidates/" + encodeURIComponent(id) + "/reject",
    {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision, reason }),
    },
  );
}
