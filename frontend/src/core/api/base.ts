import { withCsrf } from "./csrf";
import { notifyUnauthorized, shouldBroadcastUnauthorized } from "./unauthorized";

/**
 * 浏览器侧一律走同源前缀（默认 `/api`），由 nginx / Next rewrites 代理到真实网关。
 * 这样 CORS 的 `Access-Control-Expose-Headers`、SSE 的 `X-Accel-Buffering`
 * 这些都不需要前端感知（M5 §7）。
 */
export const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api").replace(/\/$/, "");

export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

/**
 * core 里所有自己发的 HTTP 都走这里。
 *
 * 两件 M9 §2 要求、漏掉就会静默失败的事：
 *
 * 1. **credentials: "same-origin"** —— JWT 只在 HttpOnly cookie `access_token` 里
 *    （§2.4），不带 cookie 就是每一条请求 401。
 * 2. **CSRF 头** —— 非 GET 请求必须带 `X-CSRF-Token`（§2.6）。
 *
 * 放在一个入口，比散在五个模块里各自记得要可靠。
 */
export async function apiFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  withCsrf(headers, init.method);
  const response = await fetch(input, { ...init, headers, credentials: "same-origin" });
  // 交给 shouldBroadcastUnauthorized 判定：只有 401、且不是认证端点，才算
  // "会话没了"。**403 不算**——它的两种含义（认证没开 / 缺 scope）都不是
  // "去登录页"，认了会把单人模式锁死（实测复现过）。
  if (shouldBroadcastUnauthorized(response.status, input)) notifyUnauthorized();
  return response;
}
