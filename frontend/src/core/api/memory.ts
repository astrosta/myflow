import { apiFetch, apiUrl } from "./base";

export interface ProfileFieldView {
  value: unknown;
  source: string;
  evidence_tier: string | null;
  confidence: number;
  observed_at: string;
}

export interface ProfileFieldOption {
  value: string;
  label: string;
}

export type ProfileFieldKind =
  | "text"
  | "locale"
  | "language"
  | "choice"
  | "multi_choice"
  | "string_list";

export interface ProfileFieldSpec {
  path: string;
  label: string;
  group: string;
  group_label: string;
  kind: ProfileFieldKind;
  description: string;
  placeholder: string;
  options: ProfileFieldOption[];
  max_length: number;
  max_items: number;
  item_max_length: number;
  explicit_only: boolean;
}

export interface ProfileSchemaView {
  schema_version: number;
  fields: ProfileFieldSpec[];
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

export async function memoryRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await apiFetch(apiUrl(path), { ...init, headers });
  if (!response.ok) throw await readError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function getProfile(): Promise<ProfileView> {
  return memoryRequest<ProfileView>("/memory/profile");
}

export function getProfileSchema(): Promise<ProfileSchemaView> {
  return memoryRequest<ProfileSchemaView>("/memory/profile/schema");
}

export function patchProfile(
  fields: Record<string, unknown>,
  expectedRevision: number,
): Promise<ProfileView> {
  return memoryRequest<ProfileView>("/memory/profile", {
    method: "PATCH",
    body: JSON.stringify({ fields, expected_revision: expectedRevision }),
  });
}

export function clearProfile(etag?: string): Promise<ProfileView> {
  return memoryRequest<ProfileView>("/memory/profile", {
    method: "DELETE",
    body: JSON.stringify(etag ? { etag } : {}),
  });
}

export function getOnboarding(): Promise<OnboardingView> {
  return memoryRequest<OnboardingView>("/memory/profile/onboarding");
}

export function updateOnboarding(action: OnboardingAction): Promise<OnboardingView> {
  return memoryRequest<OnboardingView>("/memory/profile/onboarding", {
    method: "POST",
    body: JSON.stringify({ action }),
  });
}

export function listProfileSuggestions(): Promise<{ items: ProfileSuggestion[] }> {
  return memoryRequest<{ items: ProfileSuggestion[] }>("/memory/profile/suggestions");
}

export function acceptProfileSuggestion(
  id: string,
  etag?: string,
): Promise<{ suggestion: ProfileSuggestion; profile: ProfileView }> {
  return memoryRequest<{ suggestion: ProfileSuggestion; profile: ProfileView }>(
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
  return memoryRequest<ProfileSuggestion>(
    "/memory/profile/suggestions/" + encodeURIComponent(id) + "/dismiss",
    {
      method: "POST",
      body: JSON.stringify(etag ? { etag } : {}),
    },
  );
}

export function clearProfileField(fieldPath: string): Promise<ProfileView> {
  return memoryRequest<ProfileView>(
    "/memory/profile/" + encodeURIComponent(fieldPath),
    { method: "DELETE" },
  );
}
