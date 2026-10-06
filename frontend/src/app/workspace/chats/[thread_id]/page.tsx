import type { Metadata } from "next";

import { ChatRoute } from "../../chat-route";

// 聊天页的重样式（markdown 排版、代码高亮配色、流式动画）**只在这里**被引入。
// 放到根布局会让落地页也加载它（M5 §2 的静态根边界）。
import "../../chat.css";

export const metadata: Metadata = {
  title: "会话",
};

export default async function ChatThreadPage({
  params,
}: {
  params: Promise<{ thread_id: string }>;
}) {
  const { thread_id } = await params;
  return <ChatRoute threadId={thread_id} />;
}
