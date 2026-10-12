/**
 * 未授权广播（M9 §2.5）。
 *
 * 为什么是"响应真实拒绝"而不是"进页面先查一次登录状态"：
 *
 * auth.enabled=false 时（§2.11 的本地单人模式）所有业务请求都跑在固定用户上、
 * 完全正常，但 GET /v1/auth/me 仍然会 403 session_required（实测）。
 * 如果按"没登录就跳 /login"做门禁，本地演示会直接锁死在一个登不进去的页面上。
 *
 * 所以：正常渲染，只在**后端说"你没登录"（401）时**才把人送去登录页。
 *
 * **403 不能算**。它有两种，都不是"去登录页"：
 *   session_required    认证根本没开（§2.11 单人模式）—— 拦了就是把演示锁死
 *   permission_denied   有会话但缺 scope —— 重新登录一百次还是同一个错
 * 一开始这里认的是 "401 或 403"，结果单人模式下 /me 的 403 把用户弹到了
 * 一个永远登不进去的登录页（实测复现）。
 */

import { wasSessionActive } from "../auth/session-expiry";

export interface UnauthorizedEvent {
  /** 给登录页用来说清"为什么又要登录" */
  reason: "expired" | "rejected";
}

type Listener = (event: UnauthorizedEvent) => void;

const listeners = new Set<Listener>();
let lastNotifiedAt = 0;

/**
 * 同一次过期会被**很多个并发请求**同时发现（侧栏、消息列表、流……）。
 * 不节流的话会连着触发好几次跳转，每一次都可能把用户正在做的事打断。
 */
const NOTIFY_THROTTLE_MS = 2000;

export function onUnauthorized(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/**
 * reason 默认**推导**而不是写死：之前有会话、现在被拒 → expired；
 * 一直没有会话 → rejected。见 session-expiry.ts 的 wasSessionActive。
 *
 * 显式传值只给"本地定时器到点"那条路径用——那里我们确定是过期。
 */
export function notifyUnauthorized(reason?: UnauthorizedEvent["reason"]): void {
  const now = Date.now();
  if (now - lastNotifiedAt < NOTIFY_THROTTLE_MS) return;
  lastNotifiedAt = now;
  const resolved = reason ?? (wasSessionActive() ? "expired" : "rejected");
  for (const listener of listeners) listener({ reason: resolved });
}

/**
 * 认证端点上的 401/403 是**业务答案**，不是"会话失效"。
 *
 * 登录失败（没这个邮箱 / 密码错）、改密时当前密码错、禁用账号登录……
 * 这些都是"这次提交不对"，用户还在登录页上、会话也没丢。
 * 把它们广播成 unauthorized 的表现是：点一次登录就被弹成
 * "登录状态已失效，请重新登录"——真正的原因被顶掉了。
 *
 * `/v1/auth/me` **不在**这个名单里：它是探活，401 就是"会话没了"。
 */
const AUTH_ENDPOINT = /\/v1\/auth\/(?:login\/local|register|initialize|logout|setup-status|providers|change-password)$/;

/**
 * 这个响应该不该被当成"会话没了"。
 *
 * 规则集中在这里，因为广播的调用点有三处（自己发的 fetch、SDK 的 fetch、
 * 探活），散着写迟早会漂。
 */
export function shouldBroadcastUnauthorized(status: number, url: string): boolean {
  return status === 401 && !isAuthEndpoint(url);
}

export function isAuthEndpoint(url: string): boolean {
  try {
    return AUTH_ENDPOINT.test(new URL(url, "http://localhost").pathname);
  } catch {
    return false;
  }
}

/** 测试用：把节流状态清掉 */
export function resetUnauthorizedThrottle(): void {
  lastNotifiedAt = 0;
}

/** 已经在认证页面上了就不要再跳，否则会自己打断自己 */
export function isAuthPage(pathname: string): boolean {
  return pathname === "/login" || pathname === "/register";
}
