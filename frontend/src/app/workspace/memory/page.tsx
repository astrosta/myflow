import type { Metadata } from "next";

import { MemoryLibraryPage } from "@/components/memory/memory-library-page";

export const metadata: Metadata = {
  title: "记忆管理",
};

export default function MemoryPage() {
  return <MemoryLibraryPage />;
}
