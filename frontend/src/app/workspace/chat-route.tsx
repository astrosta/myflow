"use client";

import { useEffect } from "react";

import { useChat } from "@/core/threads/use-chat";

import { useChatSession } from "./chat-session";
import { ChatPage } from "./chat-page";

/**
 * 路由容器：把 core 的状态入口（useChat）变成 ChatPage 需要的 controller。
 *
 * 分开的理由是 ChatPage 只认 ChatController 这个接口，所以它可以被
 * 塞一个假的 controller 做渲染测试，而不需要跑起 SDK 和网络。
 *
 * threadId 为 null 表示"这是一个还没落地的新会话"（/workspace）。
 * 第一次发送时 SDK 会自己建 thread，然后回调 reportThreadCreated。
 *
 * **重挂载的 key 挂在 ChatSession 上，不是 ChatRoute 上**：useChat 必须待在被
 * 重挂载的那一层里。挂在 ChatRoute 上只会重建 ChatPage，controller 里的
 * 乐观消息、历史窗口、流状态全都原地不动 —— 看起来像重置了，其实没有。
 *
 * key 由两部分拼成，各自负责一件事：
 *
 * - epoch：**在 /workspace 上再点一次"新对话"**。地址没变、路由段没变，
 *   只能靠它把会话清干净。
 * - threadId：**在会话之间切换**。/workspace/chats/[thread_id] 是同一个路由段，
 *   A → B 只是换参数，React 会复用实例——只按 epoch 做 key 的话，
 *   A 的乐观消息和已经翻过的历史页会漏到 B 里。
 *
 * 新会话第一次发送时这两个都不变（地址是 replaceState 换的，threadId 恒为
 * null、epoch 不动），所以首轮不会重挂载 —— 这正是这个模块存在的理由。
 */
export function ChatRoute({ threadId }: { threadId: string | null }) {
  const { epoch } = useChatSession();
  return <ChatSession key={epoch + ":" + (threadId ?? "new")} threadId={threadId} />;
}

function ChatSession({ threadId }: { threadId: string | null }) {
  const { reportThreadCreated, setActiveThreadId } = useChatSession();

  /**
   * 把"当前在看哪个会话"同步给侧栏。
   *
   * 只有路由段变化（进入某个既有会话、或回到 /workspace）才会重跑 ——
   * threadId 是这个实例的定值。会话刚建好时**不会**跑到这里把它清成 null，
   * 因为那条路径不发导航、本实例不重挂载。
   */
  useEffect(() => {
    setActiveThreadId(threadId);
  }, [threadId, setActiveThreadId]);

  const controller = useChat({ threadId, onThreadCreated: reportThreadCreated });
  return <ChatPage controller={controller} />;
}
