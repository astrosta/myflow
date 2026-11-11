import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";

import "./globals.css";

/**
 * 根布局（M5 §2 的"一条静态根边界"）。
 *
 * 两条硬约束，改这个文件前先读一遍：
 *
 * 1. **不许读 cookie / headers / searchParams。** 一旦读了，整个路由树从静态
 *    退化成动态渲染，落地页也要按请求现算。
 * 2. **不许 import 聊天页专用的重样式**（markdown 排版、代码高亮、流式动画）。
 *    那些挂在 app/workspace/chats/[thread_id]/chat.css 上，只在聊天路由被加载。
 */

export const metadata: Metadata = {
  title: {
    default: "MyFlow",
    template: "%s · MyFlow",
  },
  description: "越用越懂你的自托管 Agent 工作区：记忆、技能、工具、知识与评测在同一个会话里彼此接续，每次协作都留下可复用的经验。",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-full">{children}</body>
    </html>
  );
}
