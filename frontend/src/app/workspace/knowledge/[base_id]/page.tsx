import type { Metadata } from "next";

import { KnowledgeDetailPage } from "@/components/knowledge/knowledge-detail-page";

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
