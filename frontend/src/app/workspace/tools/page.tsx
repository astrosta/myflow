import type { Metadata } from "next";
import { redirect } from "next/navigation";

export const metadata: Metadata = {
  title: "MCP 服务",
};

/**
 * 旧入口：这个页面已经改名为 /workspace/mcp-servers。
 *
 * 保留成重定向而不是 404，是因为侧栏文案变了但历史书签/外部链接还在；
 * 重定向放在这里（服务端）而不是客户端 effect，免得先闪一帧空页面。
 */
export default function ToolsPage() {
  redirect("/workspace/mcp-servers");
}
