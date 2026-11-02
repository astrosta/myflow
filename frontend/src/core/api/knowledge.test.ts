import { afterEach, describe, expect, it, vi } from "vitest";

import {
  getKnowledgeDocument,
  KNOWLEDGE_ACTIVE_STATUSES,
  KNOWLEDGE_TYPES,
  knowledgeDocumentStatusLabel,
  knowledgeRetrievalModeLabel,
  knowledgeTypeLabel,
  listKnowledgeDocumentChunks,
} from "./knowledge";

describe("knowledge base wire labels", () => {
  it("exposes exactly the six backend types", () => {
    expect(KNOWLEDGE_TYPES.map((item) => item.value)).toEqual([
      "technical",
      "operations",
      "faq",
      "security",
      "api",
      "legal",
    ]);
    expect(knowledgeTypeLabel("operations")).toBe("运维手册");
    expect(knowledgeTypeLabel("legal")).toBe("法律法规");
    expect(knowledgeTypeLabel("unknown")).toBe("unknown");
  });

  it("marks every processing state as active so the detail page keeps polling", () => {
    expect([...KNOWLEDGE_ACTIVE_STATUSES].sort()).toEqual([
      "chunking",
      "embedding",
      "parsing",
      "queued",
    ]);
    expect(knowledgeDocumentStatusLabel("ready")).toBe("已就绪");
    expect(knowledgeDocumentStatusLabel("failed")).toBe("失败");
  });

  it("labels the retrieval mode actually used, not the configured one", () => {
    expect(knowledgeRetrievalModeLabel("hybrid")).toBe("混合检索");
    expect(knowledgeRetrievalModeLabel("vector")).toBe("向量检索");
  });
});

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("knowledge document wire contract", () => {
  it("loads one document and encodes both path ids", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        jsonResponse({
          id: "doc 1",
          knowledge_base_id: "base 1",
          filename: "a.md",
          mime_type: "text/markdown",
          size_bytes: 1,
          status: "ready",
          chunk_count: 0,
          error_message: null,
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-01-01T00:00:00Z",
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await getKnowledgeDocument("base 1", "doc 1");

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/knowledge/bases/base%201/documents/doc%201",
    );
  });

  it("loads a chunk page with offset and limit", async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _init?: RequestInit) =>
        jsonResponse({
          document_id: "doc-1",
          chunks: [],
          count: 0,
          next_offset: null,
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await listKnowledgeDocumentChunks("base-1", "doc-1", 20, 20);

    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/knowledge/bases/base-1/documents/doc-1/chunks?offset=20&limit=20",
    );
  });
});
