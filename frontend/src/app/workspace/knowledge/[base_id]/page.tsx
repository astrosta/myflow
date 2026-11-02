import type { Metadata } from "next";

import { KnowledgeDetailPage } from "@/components/knowledge/knowledge-detail-page";

// 检索结果按 Markdown 渲染；KaTeX 公式与排版只随详情页加载，不进静态根。
import "katex/dist/katex.min.css";
import "../../markdown.css";

export const metadata: Metadata = {
  title: "知识库详情",
};

export default async function KnowledgeBasePage({
  params,
}: {
  params: Promise<{ base_id: string }>;
}) {
  const { base_id } = await params;
  return <KnowledgeDetailPage baseId={base_id} />;
}
