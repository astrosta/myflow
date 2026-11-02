import type { Metadata } from "next";

import { KnowledgeDocumentPage } from "@/components/knowledge/knowledge-document-page";

// 切片正文里的公式与 Markdown 排版只随详情页加载，不进静态根。
import "katex/dist/katex.min.css";
import "../../../../markdown.css";

export const metadata: Metadata = {
  title: "文档详情",
};

export default async function KnowledgeDocumentRoute({
  params,
}: {
  params: Promise<{ base_id: string; document_id: string }>;
}) {
  const { base_id, document_id } = await params;
  return <KnowledgeDocumentPage baseId={base_id} documentId={document_id} />;
}
