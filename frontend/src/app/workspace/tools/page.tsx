import type { Metadata } from "next";

import { ToolManagementPage } from "@/components/tools/tool-management-page";

export const metadata: Metadata = {
  title: "工具管理",
};

export default function ToolsPage() {
  return <ToolManagementPage />;
}
