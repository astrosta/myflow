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
  description: "LangGraph Platform 兼容的 Agent 工作区：工具调用、流式续传、产物预览。",
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
