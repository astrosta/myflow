"use client";

import Link from "next/link";

import type { KnowledgeCitation } from "@/core/messages/citations";
import { citationHref } from "@/core/messages/citations";

/**
 * 结构化出处列表。只有后端校验过的来源才会出现在这里；正文里的编号链接
 * 与这个列表共用同一个顺序。
 */
export function CitationSources({ citations }: { citations: readonly KnowledgeCitation[] }) {
  if (!citations.length) return null;
  return (
    <section aria-labelledby="answer-citations-title" className="mt-3 border-t border-neutral-100 pt-2">
      <h2 className="text-[11px] font-medium text-neutral-400" id="answer-citations-title">
        来源
      </h2>
      <ol className="mt-1.5 space-y-1.5">
        {citations.map((citation, position) => (
          <li className="text-xs leading-relaxed text-neutral-600" key={citation.citation_id}>
            <Link
              className="group flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5 hover:text-neutral-900"
              href={citationHref(citation)}
            >
              <span className="font-mono text-[11px] text-neutral-400 group-hover:text-neutral-700">
                [{position + 1}]
              </span>
              <span className="font-medium">{citation.filename || citation.document_id}</span>
              {citation.section ? <span className="text-neutral-500">{citation.section}</span> : null}
              {citation.page > 0 ? (
                <span className="text-neutral-400">第 {citation.page} 页</span>
              ) : null}
              {citation.knowledge_base_name ? (
                <span className="text-[11px] text-neutral-400">{citation.knowledge_base_name}</span>
              ) : null}
            </Link>
            {citation.quote ? (
              <p className="mt-0.5 line-clamp-2 text-[11px] text-neutral-400">{citation.quote}</p>
            ) : null}
          </li>
        ))}
      </ol>
    </section>
  );
}
