import type { Metadata } from "next";

import { ChatRoute } from "../../chat-route";

// 聊天页的重样式只在这里引入；Markdown 排版由 markdown.css 与知识库详情共享。
// 放到根布局会让落地页也加载它（M5 §2 的静态根边界）。
import "katex/dist/katex.min.css";
import "../../markdown.css";
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
