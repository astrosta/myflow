"use client";

import Link from "next/link";

import { useSession } from "@/core/auth/use-session";

/**
 * 首页右上角的会话入口。
 *
 * 首页本身保持静态；这里只挂一个很小的 client island，避免把聊天页的
 * 客户端上下文带进落地页。未登录时显示「登录」，已登录时显示固定宽度的
 * 用户名，长用户名截断后补 ***。
 */
export const HOME_USERNAME_MAX_CHARS = 12;

const AUTH_ENTRY_CLASS =
  "inline-flex w-32 shrink-0 items-center justify-center overflow-hidden whitespace-nowrap transition-colors hover:text-neutral-900";

export function formatHomeUsername(username: string, maxChars = HOME_USERNAME_MAX_CHARS): string {
  const chars = Array.from(username);
  if (chars.length <= maxChars) return username;
  return chars.slice(0, maxChars).join("") + "***";
}

export function HomeAuthLink() {
  const { user } = useSession();

  if (!user) {
    return (
      <Link className={AUTH_ENTRY_CLASS} href="/login">
        登录
      </Link>
    );
  }

  return (
    <Link
      aria-label={user.username}
      className={AUTH_ENTRY_CLASS}
      href="/workspace"
      title={user.username}
    >
      {formatHomeUsername(user.username)}
    </Link>
  );
}
