import type { Metadata } from "next";

import { KnowledgeListPage } from "@/components/knowledge/knowledge-list-page";

export const metadata: Metadata = {
  title: "知识库",
};

export default function KnowledgePage() {
  return <KnowledgeListPage />;
}
