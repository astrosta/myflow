"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";

/**
 * TanStack Query 管服务端状态（M5 §2）：缓存失效、乐观更新、分页不要自己写。
 *
 * `QueryClient` 必须**每个浏览器会话一份**，所以放在 `useState` 里——
 * 模块级单例在 SSR 下会让多个请求共享缓存，那是跨用户的数据泄漏。
 */
export function QueryProvider({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            // 会话数据的"新鲜"由显式失效驱动，不靠轮询
            refetchOnWindowFocus: false,
            staleTime: 5_000,
            retry: 1,
          },
        },
      }),
  );

  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
