"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { UserIcon } from "@/components/ui/icons";
import { useSession } from "@/core/auth/use-session";

import { SettingsDialog } from "./settings-dialog";

/**
 * 侧栏左下角的用户卡片（千问 / ChatGPT 那种位置和形状）。
 *
 * 形制：头像圆点 + 用户名，整块可点开设置。
 *
 * **没有齿轮图标**：设置入口就是这张卡片本身（千问也是这么做的），
 * 再放一个齿轮等于给同一件事两个入口，把一行挤得很碎。
 *
 * **退出登录也不在这里**：它挪到了设置面板的账号栏，并且是红色的。
 * 退出是不可逆的会话结束动作，和"点开设置看看"不该挨在一起——
 * 手滑的代价不一样。
 *
 * **auth 关掉时整块不渲染**：那种模式下 /v1/auth/me 会 403 session_required，
 * user 是 null —— 显示一个空白的用户区比不显示更让人困惑。
 */
export function UserMenu() {
  const router = useRouter();
  const { user, loading, refresh, signOut } = useSession();
  const [settingsOpen, setSettingsOpen] = useState(false);

  if (loading || !user) return null;

  /**
   * 退出登录现在由设置面板触发，但**跳转留在这一层**：
   * 设置面板是个纯 UI 组件，不该知道路由怎么写。
   */
  const doSignOut = async () => {
    try {
      await signOut();
    } finally {
      router.replace("/login");
    }
  };

  return (
    <div className="shrink-0 border-t border-neutral-200 p-2" data-testid="user-menu">
      <button
        className="flex w-full items-center gap-2 rounded-md px-2 py-2 text-left transition-colors hover:bg-neutral-100"
        onClick={() => setSettingsOpen(true)}
        type="button"
      >
        {/* 头像圆点：取用户名首字母。千问那边也是这个形状 */}
        <span
          aria-hidden="true"
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-neutral-700 text-xs font-medium uppercase text-white"
        >
          {user.username.slice(0, 1)}
        </span>
        <span
          className="min-w-0 flex-1 truncate text-sm text-neutral-700"
          title={user.username + " · " + user.email}
        >
          {user.username}
        </span>
        <UserIcon className="shrink-0 text-neutral-400" size={14} />
      </button>

      {settingsOpen ? (
        <SettingsDialog
          onClose={() => setSettingsOpen(false)}
          // 改完用户名 / 退出登录之后重新拉一次 /me：它是服务端权威
          onUserUpdated={() => {
            void refresh();
          }}
          onSignOut={doSignOut}
          user={user}
        />
      ) : null}
    </div>
  );
}
