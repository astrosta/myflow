import type { Metadata } from "next";

import { McpServerManagementPage } from "@/components/mcp-servers/mcp-server-management-page";

export const metadata: Metadata = {
  title: "工具管理",
};

export default function McpServersPage() {
  return <McpServerManagementPage />;
}
