import { Client } from "@langchain/langgraph-sdk";

import { API_BASE_URL } from "./base";
import { withCsrf } from "./csrf";
import { createGapRecoveringFetch } from "./fetch-with-gap";
import { isRunCreateRequest, sanitizeRunCreateBody } from "./run-request";
import { isStreamingRunRequest, rewriteStreamModes } from "./stream-mode";

/**
 * SDK 客户端单例（M5 §2）。
 *
 * 三条接线值得说明：
 *
 * 1. **gap 恢复挂在 callerOptions.fetch 上。** 这是 SDK 消费 SSE 的唯一入口，
 *    包在这里才能同时覆盖初始流和 join 的流（§5.2）。
 *
 * 2. streamProtocol **保持默认的 legacy**：M3 的网关对齐的是经典 wire 格式
 *    （event: metadata/messages/end，字段顺序 event → data → id）。
 *
 * 3. **onRequest 是请求净化的唯一落点。** 网关的请求模型全是 extra="forbid"，
 *    SDK 自动加的开关（实测 stream_resumable）会让每一次发送都 422。
 *    放在这里而不是 hook 选项里，是因为构造 body 的地方不止一处，
 *    只有在请求层改才能保证一个都漏不掉。
 */

let singleton: Client | null = null;

/**
 * SDK 要的是**绝对** URL。
 *
 * apiUrl 传 "/api" 会让它在浏览器里执行 new URL("/api")，而相对 URL 没有 base
 * 会直接抛 TypeError: Failed to construct URL: Invalid URL ——
 * 表现是"点了发送什么也没发生"，**一个网络请求都不会发出去**。
 *
 * 仍然保持同源：只是把 /api 补成 http://<当前站点>/api，
 * 再由 next.config.ts 的 rewrites / nginx 转到网关。
 */
export function resolveApiUrl(): string {
  if (/^https?:\/\//i.test(API_BASE_URL)) return API_BASE_URL;
  if (typeof window !== "undefined") {
    return new URL(API_BASE_URL, window.location.origin).href;
  }
  // SSR 期间不会被发请求；真要发也没有 origin 可用
  return API_BASE_URL;
}

export function createApiClient(): Client {
  return new Client({
    apiUrl: resolveApiUrl(),
    apiKey: null, // 从环境变量自动加载会带来难以定位的"偶尔 401"
    callerOptions: {
      fetch: createGapRecoveringFetch(),
    },
    onRequest: (url, init) => {
      /**
       * M9 §2：SDK 发的每一条请求也要带上会话 cookie 和 CSRF 头。
       *
       * - JWT 只在 HttpOnly cookie `access_token` 里（§2.4），不带 cookie 全 401
       * - 非 GET 要带 `X-CSRF-Token`（§2.6），否则 403
       *
       * 这两件事不能只在自己那几个 fetch 上做：真正的业务请求（建 thread、
       * 起 run、取消）全是 SDK 发的，漏了它整个应用在开启认证后就是一片 401。
       */
      const headers = new Headers(init.headers);
      withCsrf(headers, init.method);
      const withAuth = { ...init, headers, credentials: "same-origin" as RequestCredentials };

      if (typeof init.body !== "string") return withAuth;
      const path = url.pathname;
      if (isRunCreateRequest(path)) {
        const sanitized = sanitizeRunCreateBody(init.body);
        return sanitized === init.body ? withAuth : { ...withAuth, body: sanitized };
      }
      if (isStreamingRunRequest(path)) {
        const rewritten = rewriteStreamModes(init.body);
        return rewritten === init.body ? withAuth : { ...withAuth, body: rewritten };
      }
      return withAuth;
    },
  });
}

export function getApiClient(): Client {
  singleton ??= createApiClient();
  return singleton;
}

/** 测试用：把单例恢复成未初始化 */
export function resetApiClient(): void {
  singleton = null;
}
