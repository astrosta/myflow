"use client";

import { Button } from "@/components/ui/button";

import { useChatSession } from "./chat-session";

/**
 * 新建会话。
 *
 * **刻意不调 threads.create()。** /workspace 本身就是一个"还没落地的新会话"
 * （对话框直接摆出来，thread 在第一次发送时才建）。在这里预建的话，
 * 用户点一下就在侧栏留下一个没人用过的空会话，而且这个纯 UI 动作
 * 还得依赖后端可用。
 *
 * **也不能交给 router.push("/workspace") 一个人做**：地址是 replaceState 换的，
 * Next 的路由树仍停在 /workspace，push 会被判定成"已经在这儿"而空转。
 * 真正的重置来自 useChatSession().newChat() 里的 epoch。
 */
export function NewChatButton({ label = "新对话" }: { label?: string }) {
  const { newChat } = useChatSession();

  return (
    <Button onClick={newChat} variant="primary">
      {label}
    </Button>
  );
}
