/**
 * 浏览器侧一律走同源前缀（默认 `/api`），由 nginx / next rewrites 代理到真实网关。
 * 这样 CORS 的 `Access-Control-Expose-Headers`、SSE 的 `X-Accel-Buffering`
 * 这些都不需要前端感知（M5 §7）。
 */
export const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api").replace(/\/$/, "");

export function apiUrl(path: string): string {
  return `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}
