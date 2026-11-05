"use client";

import Link from "next/link";

import { useSession } from "@/core/auth/use-session";

/**
 * 首页右上角的登录入口。
 *
 * 首页本身保持静态；这里只挂一个很小的 client island，避免把聊天页的
 * 客户端上下文带进落地页。未登录时显示「登录」；已登录时不渲染任何东西，
 * 右上角只留下页面自带的「工作区」入口。
 */
export function HomeAuthLink() {
  const { user } = useSession();

  if (user) return null;

  return (
    <Link className="transition-colors hover:text-neutral-900" href="/login">
      登录
    </Link>
  );
}
