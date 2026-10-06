import type { ReactNode } from "react";

import { QueryProvider } from "@/core/api/query-provider";

import { SidebarContainer } from "./sidebar-container";

/**
 * 工作区外壳：左侧会话列表 + 右侧内容区。
 *
 * QueryProvider 挂在这里而不是根布局：TanStack Query 只有工作区在用，
 * 挂在根上等于把落地页也拖进一个它不需要的客户端上下文（M5 §2 的静态根边界）。
 */
export default function WorkspaceLayout({ children }: { children: ReactNode }) {
  return (
    <QueryProvider>
      <div className="flex h-dvh w-full overflow-hidden">
        <SidebarContainer />
        <main className="flex min-w-0 flex-1 flex-col">{children}</main>
      </div>
    </QueryProvider>
  );
}
