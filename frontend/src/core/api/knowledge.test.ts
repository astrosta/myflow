import { describe, expect, it } from "vitest";

import {
  KNOWLEDGE_ACTIVE_STATUSES,
  KNOWLEDGE_TYPES,
  knowledgeDocumentStatusLabel,
  knowledgeRetrievalModeLabel,
  knowledgeTypeLabel,
} from "./knowledge";

describe("knowledge base wire labels", () => {
  it("exposes exactly the five backend types", () => {
    expect(KNOWLEDGE_TYPES.map((item) => item.value)).toEqual([
      "technical",
      "operations",
      "faq",
      "security",
      "api",
    ]);
    expect(knowledgeTypeLabel("operations")).toBe("运维手册");
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
