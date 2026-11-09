import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../types";
import {
  citationHref,
  readKnowledgeCitations,
  withCitationLinks,
  type KnowledgeCitation,
} from "./citations";

const citation: KnowledgeCitation = {
  citation_id: "k1234abcd",
  chunk_id: "doc-1:3",
  document_id: "doc-1",
  knowledge_base_id: "base 1",
  knowledge_base_name: "产品文档",
  knowledge_base_type: "technical",
  filename: "manual.md",
  section: "安装",
  page: 7,
  chunk_index: 3,
  quote: "升级前先备份配置。",
};

function message(additional: Record<string, unknown>): ChatMessage {
  return { id: "m-1", type: "ai", content: "答案", additional_kwargs: additional };
}

describe("知识库引用", () => {
  it("从最终消息的 additional_kwargs 读取结构化来源", () => {
    expect(readKnowledgeCitations(message({ knowledge_citations: [citation] }))).toEqual([
      citation,
    ]);
  });

  it("忽略畸形的来源，不影响其余引用", () => {
    const malformed = { ...citation, citation_id: "not-a-citation" };
    expect(
      readKnowledgeCitations(message({ knowledge_citations: [malformed, citation, "x"] })),
    ).toEqual([citation]);
  });

  it("来源链接指向文档切片锚点", () => {
    expect(citationHref(citation)).toBe(
      "/workspace/knowledge/base%201/documents/doc-1#chunk-3",
    );
  });

  it("把已知标记变成可点击的编号链接，并清掉伪造标记", () => {
    const rendered = withCitationLinks(
      "升级前先备份 [k1234abcd]，伪造 [kdeadbeef]。",
      [citation],
    );

    expect(rendered).toContain("[\\[1\\]](/workspace/knowledge/base%201/documents/doc-1#chunk-3)");
    expect(rendered).not.toContain("kdeadbeef");
    expect(rendered).toContain("升级前先备份");
  });

  it("没有引用时不动普通 Markdown", () => {
    expect(withCitationLinks("普通 [链接](https://example.com)", [])).toBe(
      "普通 [链接](https://example.com)",
    );
  });
});
