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
  etag?: string;
  updated_at?: string;
  fields: Record<string, ProfileFieldView>;
}

export interface OnboardingView {
  status: "not_started" | "seen" | "snoozed" | "completed" | "dismissed";
  should_show: boolean;
  profile_field_count: number;
  snooze_until: string | null;
  dismissed_at: string | null;
  updated_at: string;
}

export type OnboardingAction = "seen" | "snooze" | "dismiss" | "complete";

export interface ProfileSuggestion {
  id: string;
  field_path: string;
  value: unknown;
  evidence_uri: string;
  evidence_quote: string;
  confidence: number;
  reason: string;
  status: string;
  profile_etag: string;
  expires_at: string;
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

export function getOnboarding(): Promise<OnboardingView> {
  return request<OnboardingView>("/memory/profile/onboarding");
}

export function updateOnboarding(action: OnboardingAction): Promise<OnboardingView> {
  return request<OnboardingView>("/memory/profile/onboarding", {
    method: "POST",
    body: JSON.stringify({ action }),
  });
}

export function listProfileSuggestions(): Promise<{ items: ProfileSuggestion[] }> {
  return request<{ items: ProfileSuggestion[] }>("/memory/profile/suggestions");
}

export function acceptProfileSuggestion(
  id: string,
  etag?: string,
): Promise<{ suggestion: ProfileSuggestion; profile: ProfileView }> {
  return request<{ suggestion: ProfileSuggestion; profile: ProfileView }>(
    "/memory/profile/suggestions/" + encodeURIComponent(id) + "/accept",
    {
      method: "POST",
      body: JSON.stringify(etag ? { etag } : {}),
    },
  );
}

export function dismissProfileSuggestion(
  id: string,
  etag?: string,
): Promise<ProfileSuggestion> {
  return request<ProfileSuggestion>(
    "/memory/profile/suggestions/" + encodeURIComponent(id) + "/dismiss",
    {
      method: "POST",
      body: JSON.stringify(etag ? { etag } : {}),
    },
  );
}

export function clearProfileField(fieldPath: string): Promise<ProfileView> {
  return request<ProfileView>(
    "/memory/profile/" + encodeURIComponent(fieldPath),
    { method: "DELETE" },
  );
}
