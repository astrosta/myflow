import { apiFetch, apiUrl } from "./base";
import { MemoryApiError, memoryRequest } from "./memory";

export interface MemoryTypeView {
  type: string;
  label: string;
}

export interface MemoryItemView {
  uri: string;
  name: string;
  folder: string;
  memory_type: string;
  abstract: string;
  abstract_truncated: boolean;
  updated_at: string;
}

export interface MemoryListPageView {
  type: string;
  items: MemoryItemView[];
  has_more: boolean;
  limit: number;
}

export interface MemoryTocEntryView {
  kind: "heading" | "link";
  title: string;
  depth: number;
  uri: string | null;
  navigable: boolean;
}

export interface MemoryDetailView {
  uri: string;
  folder: string;
  memory_type: string;
  name: string;
  overview: string;
  toc: MemoryTocEntryView[];
  overview_missing: boolean;
}

export interface MemoryContentView {
  uri: string;
  content: string;
  sha256: string;
  size_bytes: number;
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined) continue;
    search.set(key, String(value));
  }
  return "?" + search.toString();
}

export function listMemoryTypes(): Promise<{ items: MemoryTypeView[] }> {
  return memoryRequest<{ items: MemoryTypeView[] }>("/memory/items/types");
}

export function listMemoryItems(
  type: string,
  limit = 50,
): Promise<MemoryListPageView> {
  return memoryRequest<MemoryListPageView>("/memory/items" + query({ type, limit }));
}

export function getMemoryDetail(uri: string): Promise<MemoryDetailView> {
  return memoryRequest<MemoryDetailView>("/memory/items/detail" + query({ uri }));
}

export function getMemoryContent(uri: string): Promise<MemoryContentView> {
  return memoryRequest<MemoryContentView>("/memory/items/content" + query({ uri }));
}

export function saveMemoryContent(
  uri: string,
  content: string,
  expectedSha256: string,
): Promise<MemoryContentView> {
  return memoryRequest<MemoryContentView>("/memory/items/content", {
    method: "PUT",
    body: JSON.stringify({
      uri,
      content,
      expected_sha256: expectedSha256,
    }),
  });
}

export async function deleteMemoryItem(uri: string): Promise<void> {
  const response = await apiFetch(apiUrl("/memory/items" + query({ uri })), {
    method: "DELETE",
  });
  if (!response.ok) {
    let code: string | null = null;
    let detail = "HTTP " + response.status;
    try {
      const body = (await response.json()) as Record<string, unknown>;
      if (typeof body.code === "string") code = body.code;
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error page */
    }
    throw new MemoryApiError(response.status, code, detail);
  }
}
