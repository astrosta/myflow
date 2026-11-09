"use client";

import { FormEvent, useState } from "react";

import { Button } from "@/components/ui/button";
import { Markdown } from "@/components/workspace/markdown";
import {
  knowledgeRetrievalModeLabel,
  type KnowledgeSearchHit,
  type KnowledgeSearchResult,
} from "@/core/api/knowledge";

export interface KnowledgeSearchProps {
  onSearch: (query: string, keyword: string) => Promise<KnowledgeSearchResult>;
}

const INPUT_CLASS =
  "h-9 min-w-0 flex-1 rounded-md border border-neutral-300 bg-white px-3 text-sm text-neutral-900 focus:border-neutral-500 focus:outline-none";

export function KnowledgeSearch({ onSearch }: KnowledgeSearchProps) {
  const [query, setQuery] = useState("");
  const [keyword, setKeyword] = useState("");
  const [result, setResult] = useState<KnowledgeSearchResult | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const value = query.trim();
    if (!value || searching) return;
    setSearching(true);
    setError(null);
    try {
      setResult(await onSearch(value, keyword.trim()));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSearching(false);
    }
  };

  return (
    <section aria-labelledby="knowledge-search-title" className="space-y-3">
      <div>
        <h2 className="text-sm font-semibold text-neutral-900" id="knowledge-search-title">
          检索测试
        </h2>
        <p className="mt-0.5 text-xs text-neutral-500">
          验证已经入库的内容。问题走向量召回，关键词走全文召回，两路融合后再排序。
          这里只查当前知识库，正式对话由模型按类型路由。
        </p>
      </div>
      <form className="space-y-2" onSubmit={(event) => void submit(event)}>
        <div className="flex gap-2">
          <input
            aria-label="检索问题"
            className={INPUT_CLASS}
            disabled={searching}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="例如：如何配置高可用？"
            value={query}
          />
          <Button
            disabled={searching || !query.trim()}
            size="sm"
            type="submit"
            variant="primary"
          >
            {searching ? "检索中…" : "检索"}
          </Button>
        </div>
        <input
          aria-label="关键词（可选）"
          className={INPUT_CLASS + " w-full"}
          disabled={searching}
          onChange={(event) => setKeyword(event.target.value)}
          placeholder="关键词（可选）：精确词、编号、命令或错误码，如 E1024 回滚"
          value={keyword}
        />
      </form>

      {error ? (
        <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700" role="alert">
          {error}
        </p>
      ) : null}

      {result ? (
        <div className="space-y-2" aria-live="polite">
          <RetrievalModeLine result={result} />
          {result.results.length === 0 ? (
            <p className="rounded-lg border border-neutral-200 bg-white px-3 py-4 text-xs text-neutral-500">
              没有找到相关内容。
            </p>
          ) : (
            result.results.map((hit) => <SearchHitItem hit={hit} key={hit.chunk_id} />)
          )}
          {result.truncated ? (
            <p className="text-[11px] text-neutral-400">结果超过上下文预算，已截断展示。</p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function RetrievalModeLine({ result }: { result: KnowledgeSearchResult }) {
  const parts = [knowledgeRetrievalModeLabel(result.retrieval_mode)];
  parts.push(result.reranked ? "已精排" : "融合排序");
  if (result.keyword_query && result.keyword_query !== result.query) {
    parts.push("关键词：" + result.keyword_query);
  }
  return <p className="text-[11px] text-neutral-500">{parts.join(" · ")}</p>;
}

function SearchHitItem({ hit }: { hit: KnowledgeSearchHit }) {
  return (
    <article className="rounded-lg border border-neutral-200 bg-white p-3">
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-neutral-500">
        <span className="font-medium text-neutral-800">{hit.filename}</span>
        {hit.section ? <span>{hit.section}</span> : null}
        {hit.page > 0 ? <span>第 {hit.page} 页</span> : null}
        {hit.citation_id ? (
          <span className="font-mono text-[10px] text-neutral-400">{hit.citation_id}</span>
        ) : null}
        <span className="ml-auto tabular-nums">相关度 {hit.score.toFixed(3)}</span>
      </div>
      <Markdown className="knowledge-search-md mt-2" text={hit.content} />
    </article>
  );
}
