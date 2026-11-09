import type { ChatMessage } from "../types";

/** The citation contract stored in an AI message's additional_kwargs. */
export interface KnowledgeCitation {
  citation_id: string;
  chunk_id: string;
  document_id: string;
  knowledge_base_id: string;
  knowledge_base_name: string;
  knowledge_base_type: string;
  filename: string;
  section: string;
  page: number;
  chunk_index: number;
  quote: string;
}

export const KNOWLEDGE_CITATIONS_KEY = "knowledge_citations";

const CITATION_ID = /^k[0-9a-f]{8}$/;
const CITATION_MARKER = /\[(k[0-9a-f]{8})\]/g;

/**
 * Read the structured sources attached by the lead's citation middleware.
 *
 * Malformed entries are dropped instead of making the whole answer look like it
 * has no sources. The component renders only what survives this check.
 */
export function readKnowledgeCitations(message: ChatMessage): KnowledgeCitation[] {
  const raw = message.additional_kwargs?.[KNOWLEDGE_CITATIONS_KEY];
  if (!Array.isArray(raw)) return [];
  const output: KnowledgeCitation[] = [];
  const seen = new Set<string>();
  for (const value of raw) {
    const citation = normalizeCitation(value);
    if (!citation || seen.has(citation.citation_id)) continue;
    seen.add(citation.citation_id);
    output.push(citation);
  }
  return output;
}

export function citationHref(citation: KnowledgeCitation): string {
  const base =
    "/workspace/knowledge/" +
    encodeURIComponent(citation.knowledge_base_id) +
    "/documents/" +
    encodeURIComponent(citation.document_id);
  return base + "#chunk-" + String(citation.chunk_index);
}

/**
 * Replace citation markers with numbered Markdown links.
 *
 * The number is the source's position in this answer's manifest, not the opaque
 * citation ID. An unknown marker is removed: leaving it visible would present a
 * hallucinated source as if it were real.
 */
export function withCitationLinks(
  text: string,
  citations: readonly KnowledgeCitation[],
): string {
  const index = new Map(citations.map((citation, position) => [citation.citation_id, position + 1]));
  return text.replace(CITATION_MARKER, (marker, id: string) => {
    const number = index.get(id);
    if (number === undefined) return "";
    return "[\\[" + String(number) + "\\]](" + citationHref(citations[number - 1]!) + ")";
  });
}

function normalizeCitation(value: unknown): KnowledgeCitation | null {
  if (typeof value !== "object" || value === null) return null;
  const row = value as Record<string, unknown>;
  const citationId = text(row.citation_id);
  if (!CITATION_ID.test(citationId)) return null;
  const chunkId = text(row.chunk_id);
  const documentId = text(row.document_id);
  const knowledgeBaseId = text(row.knowledge_base_id);
  if (!chunkId || !documentId || !knowledgeBaseId) return null;
  return {
    citation_id: citationId,
    chunk_id: chunkId,
    document_id: documentId,
    knowledge_base_id: knowledgeBaseId,
    knowledge_base_name: text(row.knowledge_base_name),
    knowledge_base_type: text(row.knowledge_base_type),
    filename: text(row.filename),
    section: text(row.section),
    page: nonNegativeInt(row.page),
    chunk_index: nonNegativeInt(row.chunk_index),
    quote: text(row.quote),
  };
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function nonNegativeInt(value: unknown): number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : 0;
}
