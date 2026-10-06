"use client";

import { useRouter } from "next/navigation";
import { useCallback } from "react";

import { useChat } from "@/core/threads/use-chat";

import { ChatPage } from "./chat-page";

/**
 * 路由容器：把 core 的状态入口（useChat）变成 ChatPage 需要的 controller。
 *
 * 分开的理由是 ChatPage 只认 ChatController 这个接口，所以它可以被
 * 塞一个假的 controller 做渲染测试，而不需要跑起 SDK 和网络。
 *
 * `threadId` 为 `null` 表示"这是一个还没落地的新会话"（`/workspace`）。
 * 第一次发送时 SDK 会自己建 thread，然后走下面的 `onThreadCreated`。
 */
export function ChatRoute({ threadId }: { threadId: string | null }) {
  const router = useRouter();

  /**
   * thread 建好之后把地址换成 `/workspace/chats/{id}`。
   *
   * **必须走 `router.replace`，不能用 `history.replaceState`。**
   *
   * 我一开始用 replaceState，图的是"不重新挂载、不打断正在流的这一轮"。
   * 但那样会造成一个**内外不一致**的状态：地址栏是 `/workspace/chats/{id}`，
   * 而 Next 内部的路由树还停在 `/workspace`。于是侧栏那个
   * `<a href="/workspace">新对话</a>` 点下去时，Next 一比"我已经在 /workspace 了"，
   * 直接 no-op —— 按钮看起来完全失灵。
   *
   * 换成 router.replace 之后两边一致，"新对话"能正常导航。
   * 代价是这个组件会重新挂载一次，但 `reconnectOnMount` 会重新 join
   * 正在跑的 run（M5 §5.1 就是为这个场景设计的），所以流不会丢。
   */
  const handleThreadCreated = useCallback(
    (createdThreadId: string) => {
      router.replace(`/workspace/chats/${createdThreadId}`, { scroll: false });
    },
    [router],
  );

  const controller = useChat({ threadId, onThreadCreated: handleThreadCreated });
  return <ChatPage controller={controller} />;
}
