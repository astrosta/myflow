import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { KnowledgeSearchResult } from "@/core/api/knowledge";

import { KnowledgeSearch } from "./knowledge-search";

afterEach(cleanup);

function result(content: string): KnowledgeSearchResult {
  return {
    query: "如何回滚",
    keyword_query: "",
    retrieval_mode: "vector",
    reranked: false,
    truncated: false,
    results: [
      {
        chunk_id: "chunk-1",
        document_id: "doc-1",
        filename: "runbook.md",
        section: "回滚",
        page: 2,
        chunk_index: 0,
        score: 0.91,
        knowledge_base_id: "kb-1",
        knowledge_base_name: "运维手册",
        knowledge_base_type: "operations",
        content,
        rrf_score: null,
        rerank_score: null,
      },
    ],
  };
}

describe("知识库检索测试", () => {
  it("检索结果按 Markdown 渲染，而不是直接回显源码", async () => {
    const onSearch = vi.fn().mockResolvedValue(
      result("## 回滚步骤\n\n- **先停止写入**\n- 执行 `E1024` 回滚命令"),
    );

    render(<KnowledgeSearch onSearch={onSearch} />);

    fireEvent.change(screen.getByLabelText("检索问题"), {
      target: { value: "如何回滚" },
    });
    fireEvent.click(screen.getByRole("button", { name: "检索" }));

    expect(await screen.findByRole("heading", { name: "回滚步骤", level: 2 })).toBeTruthy();
    expect(screen.getByText("先停止写入").tagName).toBe("STRONG");
    expect(screen.getByText("E1024").tagName).toBe("CODE");
    expect(screen.queryByText(/## 回滚步骤/)).toBeNull();
  });

  it("检索结果里的 LaTeX 公式按 KaTeX 渲染", async () => {
    const onSearch = vi.fn().mockResolvedValue(
      result(
        "SM2 算法私钥数据格式的 ASN.1 定义为:\n\n" +
          "$$ \\mathrm { S M 2 P r i v a t e K e y } \\ { \\scriptstyle { : } { \\ : } } = \\mathrm { I N T E G E R } $$",
      ),
    );

    render(<KnowledgeSearch onSearch={onSearch} />);

    fireEvent.change(screen.getByLabelText("检索问题"), {
      target: { value: "SM2 私钥格式" },
    });
    fireEvent.click(screen.getByRole("button", { name: "检索" }));

    await screen.findByText(/SM2 算法私钥数据格式/);
    expect(document.querySelector(".katex-display")).not.toBeNull();
    expect(document.querySelector(".katex-html")).not.toBeNull();
  });
});
