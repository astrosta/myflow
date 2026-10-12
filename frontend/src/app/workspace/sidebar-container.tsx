"use client";

import { useQuery } from "@tanstack/react-query";
import { usePathname } from "next/navigation";

import { Sidebar } from "@/components/workspace/sidebar";
import { UserMenu } from "@/components/workspace/user-menu";
import { listThreads } from "@/core/api/threads";
import type { ThreadSummary } from "@/core/types";

import { useChatSession } from "./chat-session";

/**
 * 会话列表的取数容器。
 *
 * 展示组件（Sidebar）不认识 SDK，容器才认识——这样列表的所有服务端状态
 * （缓存、失效、分页）都留在 TanStack Query 一层，而不是散进组件里。
 *
 * 网关地址不在这里硬编码：走 core 的客户端单例，浏览器侧一律同源 /api（M5 §7）。
 */

const THREAD_LIST_LIMIT = 50;

/**
 * 会话列表。
 *
 * **走 core 自己的 listThreads，不走 SDK 的 client.threads.search。**
 * SDK 按 LangGraph Platform 的形状发 offset / sortBy / sortOrder，
 * 而网关的 ThreadSearchRequest 是 extra="forbid"，每次都 422
 * （实测 {"loc":["body","offset"],"msg":"Extra inputs are not permitted"}）。
 * 网关真正认的是 { metadata?, status?, limit?, cursor?, order? }。
 */
async function fetchThreads(): Promise<ThreadSummary[]> {
  const page = await listThreads({ limit: THREAD_LIST_LIMIT, order: "desc" });

  return page.data.map((thread) => {
    const metadata = thread.metadata ?? {};
    // 权威标题是 threads_meta.display_name（M4 §4.1 的投影）。
    // 拿不到就交给展示层显示"新对话"，不要在这里编一个占位标题。
    const displayName =
      typeof thread.display_name === "string" && thread.display_name
        ? thread.display_name
        : typeof metadata.title === "string" && metadata.title
          ? metadata.title
          : null;

    return {
      thread_id: thread.thread_id,
      display_name: displayName,
      created_at: thread.created_at ?? null,
      updated_at: thread.updated_at ?? null,
      metadata,
    };
  });
}

const THREAD_ID_IN_PATH = /\/workspace\/chats\/([^/?#]+)/;

export function SidebarContainer() {
  const pathname = usePathname();
  const { activeThreadId: sessionThreadId, newChat } = useChatSession();
  const query = useQuery({
    queryKey: ["threads"],
    queryFn: fetchThreads,
    staleTime: 10_000,
  });

  const matched = pathname ? THREAD_ID_IN_PATH.exec(pathname) : null;
  const pathThreadId = matched?.[1] ? decodeURIComponent(matched[1]) : null;

  /**
   * 高亮来源有先后：**会话状态优先，路径兜底**。
   *
   * 新会话建好之后地址是 replaceState 换的，Next 的 usePathname() 还停在
   * /workspace（那正是为了不重挂载）—— 只看路径的话，自己刚发的会话不会高亮。
   * 反过来，首帧里 ChatSession 的 effect 还没跑，会话状态还是 null，
   * 这时靠路径把既有会话的高亮先画出来，避免闪一下。
   */
  const activeThreadId = sessionThreadId ?? pathThreadId;

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/**
       * Sidebar 的根是 <aside class="h-full"> —— h-full 是"父容器高度的 100%"。
       * 直接和 UserMenu 并列放在 flex 里，它会吃掉整个高度，把用户区顶到
       * 视口外面（实测 y=809，视口才 800）。所以必须套一层 flex-1 + min-h-0，
       * 让 aside 只占"剩下的"高度。
       */}
      <div className="min-h-0 flex-1">
        <Sidebar
          activeThreadId={activeThreadId}
          error={query.error ? (query.error instanceof Error ? query.error.message : String(query.error)) : null}
          loading={query.isLoading}
          onNewChat={newChat}
          onRefresh={() => {
            void query.refetch();
          }}
          threads={query.data ?? []}
        />
      </div>
      <UserMenu />
    </div>
  );
}
