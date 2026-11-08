import type { Metadata } from "next";

import { McpServerManagementPage } from "@/components/mcp-servers/mcp-server-management-page";

export const metadata: Metadata = {
  title: "MCP 服务",
};

export default function McpServersPage() {
  return <McpServerManagementPage />;
}
