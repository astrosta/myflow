import type { Metadata } from "next";

import { PendingMemoryPage } from "@/components/memory/pending-memory-page";

export const metadata: Metadata = {
  title: "待确认记忆",
};

export default function PendingMemoryRoute() {
  return <PendingMemoryPage />;
}
