import type { ReactNode } from "react";

import { QueryProvider } from "@/core/api/query-provider";
import { AuthGuard } from "@/core/auth/auth-guard";
import { SessionGate } from "@/core/auth/session-gate";

import { ChatSessionProvider } from "./chat-session";
import { SidebarContainer } from "./sidebar-container";

/**
 * 工作区外壳：左侧会话列表 + 右侧内容区。
 *
 * QueryProvider 挂在这里而不是根布局：TanStack Query 只有工作区在用，
 * 挂在根上等于把落地页也拖进一个它不需要的客户端上下文（M5 §2 的静态根边界）。
 *
 * ChatSessionProvider 也必须在这一层：它要同时被侧栏（高亮 + 新对话）和
 * 内容区的 ChatRoute（上报当前会话）读到，而这两块分属不同的 children。
 */
export default function WorkspaceLayout({ children }: { children: ReactNode }) {
  return (
    <QueryProvider>
      {/**
       * 挂在这里而不是根布局：落地页和登录页不该被会话逻辑拖进客户端上下文。
       * 它不渲染任何东西，只订阅"后端拒绝了"这件事。
       */}
      <AuthGuard />
      {/**
       * SessionGate 必须包住整个外壳，不能只包 children：
       * 侧栏自己也会发请求（拉会话列表），不拦住它就会先闪一堆 401。
       */}
      <SessionGate>
        <ChatSessionProvider>
          <div className="flex h-dvh w-full overflow-hidden">
            <SidebarContainer />
            <main className="flex min-w-0 flex-1 flex-col">{children}</main>
          </div>
        </ChatSessionProvider>
      </SessionGate>
    </QueryProvider>
  );
}
