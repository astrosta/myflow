"use client";

import { useCallback, useEffect, useState } from "react";

import {
  logout as logoutRequest,
  probeSession,
  type AuthUser,
  type SessionProbe,
} from "../api/auth";
import { clearSessionActive, clearSessionExpiry, markSessionActive } from "./session-expiry";

/**
 * 当前会话。
 *
 * status 保留了探测的三态（见 api/auth 的 SessionProbe）：
 * "未登录"和"认证没开"必须分开，前者的正确反应是跳登录页，后者是照常放行。
 */
export interface SessionState {
  user: AuthUser | null;
  status: SessionProbe["status"] | "loading";
  loading: boolean;
  refresh: () => Promise<void>;
  signOut: () => Promise<void>;
}

export function useSession(): SessionState {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [status, setStatus] = useState<SessionProbe["status"] | "loading">("loading");

  const apply = useCallback((probe: SessionProbe) => {
    setStatus(probe.status);
    if (probe.status === "authenticated") {
      setUser(probe.user);
      // 记下"刚才登录着"：401 的 reason 靠它推导（见 session-expiry.ts）
      markSessionActive();
    } else if (probe.status !== "error") {
      setUser(null);
    }
  }, []);

  /**
   * 首屏探一次。
   *
   * 请求内联在 effect 里而不是调一个 refresh()：eslint 的
   * react-hooks/set-state-in-effect 会把"effect 里同步调用一个含 setState 的
   * 函数"当成级联渲染。内联成 async IIFE 之后 setState 明确落在 await 之后。
   */
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const probe = await probeSession();
      if (!cancelled) apply(probe);
    })();
    return () => {
      cancelled = true;
    };
  }, [apply]);

  const refresh = useCallback(async () => {
    apply(await probeSession());
  }, [apply]);

  const signOut = useCallback(async () => {
    try {
      await logoutRequest();
    } finally {
      setUser(null);
      setStatus("anonymous");
      clearSessionActive();
      clearSessionExpiry();
    }
  }, []);

  return { user, status, loading: status === "loading", refresh, signOut };
}
