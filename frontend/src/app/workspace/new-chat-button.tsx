"use client";

import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";

/**
 * 新建会话。
 *
 * **刻意不调 `threads.create()`。** `/workspace` 本身就是一个"还没落地的新会话"
 * （对话框直接摆出来，thread 在第一次发送时才建）。在这里预建的话，
 * 用户点一下就在侧栏留下一个没人用过的空会话，而且这个纯 UI 动作
 * 还得依赖后端可用。
 */
export function NewChatButton({ label = "新对话" }: { label?: string }) {
  const router = useRouter();

  return (
    <Button onClick={() => router.push("/workspace")} variant="primary">
      {label}
    </Button>
  );
}
