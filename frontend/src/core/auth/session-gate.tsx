"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { isAuthPage } from "../api/unauthorized";
import { useSession } from "./use-session";

/**
 * 进工作区的**主动门禁**（M9 §2.5）。
 *
 * 和 AuthGuard 的分工：
 *
 *   SessionGate（这里）  进页面前先问一次"我是谁"，未登录直接跳，页面根本不渲染
 *   AuthGuard            会话在使用过程中失效（TTL 到点、token_version 被顶掉）时兜底
 *
 * 两者都需要：只有被动的那条，用户会先看到工作区闪一下、侧栏报一堆 401、再被弹走。
 *
 * **只在 anonymous 时拦**。disabled 表示认证根本没开（§2.11 的单人模式），
 * 那时 /me 返回 403 session_required 而所有业务请求都正常——拦了就是把本地演示
 * 锁死在一个登不进去的登录页上。
 *
 * error（网络 / 5xx）也不拦：把可能已经登录的人踢去登录页，比让他多等一会儿更糟。
 */
export function SessionGate({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { status, loading } = useSession();

  const blocked = !loading && status === "anonymous" && !isAuthPage(pathname);

  useEffect(() => {
    if (!blocked) return;
    // 带上 next：登录完回到他本来要去的地方，而不是一律丢回 /workspace
    router.replace("/login?next=" + encodeURIComponent(pathname || "/workspace"));
  }, [blocked, router, pathname]);

  // 探测期间**什么都不渲染**：先渲染工作区再跳走，等于让用户看一眼空壳
  if (loading || blocked) {
    return (
      <div className="flex h-dvh items-center justify-center bg-white">
        <p className="text-xs text-neutral-400" role="status">
          正在检查登录状态…
        </p>
      </div>
    );
  }

  return <>{children}</>;
}
