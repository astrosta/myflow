"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { fetchCurrentUser } from "../api/auth";
import { isAuthPage, notifyUnauthorized, onUnauthorized } from "../api/unauthorized";
import { clearSessionActive, clearSessionExpiry, sessionExpiresAt } from "./session-expiry";

/** 超过这个时长的到期时间不挂定时器（浏览器定时器也别挂几十小时） */
const MAX_TIMER_MS = 6 * 60 * 60 * 1000;

/** 不知道到期时刻时的探活间隔 */
const PROBE_INTERVAL_MS = 30_000;

/**
 * 把"会话没了"翻译成"去登录页，并且记得回来"。
 *
 * 三条触发路径，缺一不可：
 *
 * 1. **主动（定时器）**：登录响应给了 expires_in，到点直接跳。
 *    没有它的话，用户会先看到界面上闪一堆 401 报错、再被弹走。
 * 2. **主动（探活）**：`register` / `initialize` 的响应是 UserResponse，
 *    **没有 expires_in**（§2.12 只有 LoginResponse 有），所以那条路径上
 *    我们不知道什么时候过期。那就定期问一次 /me——用户在页面上什么都不做的
 *    时候，光靠"下一个请求失败"是永远发现不了过期的。
 * 3. **被动**：真的被后端拒了（token 被 token_version 顶掉、服务端换了
 *    secret……），这时没有任何本地信号可依赖。
 *
 * 跳转带两个 query：
 *   reason=expired   登录页据此说"登录已过期"，而不是干巴巴一个登录框
 *   next=<原来在哪>   登录完回到原处，而不是每次都被丢回 /workspace
 *
 * 节流在 notifyUnauthorized 里（同一次过期会被多个并发请求同时发现）。
 */
export function AuthGuard() {
  const router = useRouter();
  const pathname = usePathname();
  const onAuthPage = isAuthPage(pathname);

  // 被动：后端拒绝
  useEffect(() => {
    return onUnauthorized((event) => {
      if (isAuthPage(pathname)) return; // 已经在那儿了，别自己打断自己
      const next = encodeURIComponent(pathname || "/workspace");
      router.replace("/login?reason=" + event.reason + "&next=" + next);
    });
  }, [router, pathname]);

  /**
   * 主动。两种手段二选一：
   * 知道到期时刻就挂一次性定时器（精确、零轮询）；不知道才轮询探活。
   */
  useEffect(() => {
    if (onAuthPage) return;
    const at = sessionExpiresAt();

    if (at !== null) {
      const delay = at - Date.now();
      if (delay > MAX_TIMER_MS) return;
      const timer = window.setTimeout(() => notifyUnauthorized("expired"), Math.max(0, delay));
      return () => window.clearTimeout(timer);
    }

    // 不知道到期时刻（注册 / 初始化那条路径）。探活本身就会走 apiFetch，
    // 401 时它会自己广播，所以这里不需要判断返回值。
    const timer = window.setInterval(() => {
      void fetchCurrentUser().catch(() => {});
    }, PROBE_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [onAuthPage]);

  // 到了登录页就清掉本地到期时间，不然下次会拿旧值判断"是不是过期了"
  useEffect(() => {
    if (pathname === "/login") {
      clearSessionExpiry();
      clearSessionActive();
    }
  }, [pathname]);

  return null;
}
