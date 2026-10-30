"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { AuthShell, authButtonClass, authInputClass } from "@/components/auth/auth-shell";
import { register } from "@/core/api/auth";
import { authErrorMessage } from "@/core/auth/error-messages";
import { markSessionActive } from "@/core/auth/session-expiry";
import { validateUsername } from "@/core/auth/username";

/** 后端 RegisterRequest 的密码下限（M9 §2.12） */
const MIN_PASSWORD = 8;

/**
 * 自助注册（M9 §2.12）。
 *
 * 只收用户名 + 密码；用户名是唯一登录标识，后端保证不重复。
 * 从登录页过来时可能带 `?username=`，预填用户刚输入的名字。
 * 注册成功会直接种下 cookie（后端返回 201 + Set-Cookie），不用再登录一次。
 */
export function RegisterForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [username, setUsername] = useState(params.get("username") ?? "");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const usernameError = username ? validateUsername(username) : null;

  const submit = async () => {
    if (busy || !username || password.length < MIN_PASSWORD) return;
    if (usernameError) {
      setError(usernameError);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await register(username, password);
      markSessionActive();
      // 注册响应体是 UserResponse，没有 expires_in；等被动检测。
      router.replace("/workspace");
    } catch (cause) {
      setError(authErrorMessage(cause));
      setBusy(false);
    }
  };

  return (
    <AuthShell
      title="注册"
      description="创建一个账号。注册成功后直接进入工作区。"
      footer={
        <>
          已经有账号了？
          <Link className="ml-1 underline hover:text-neutral-600" href="/login">
            去登录
          </Link>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-2">
          <label className="block text-sm font-medium text-neutral-700" htmlFor="register-username">用户名</label>
          <input
            autoComplete="username"
            className={authInputClass}
            disabled={busy}
            id="register-username"
            onChange={(e) => setUsername(e.target.value)}
            type="text"
            value={username}
          />
          <p className="text-xs text-neutral-400">用于登录，注册后也可在设置里修改；不能与已有用户重名。</p>
        </div>
        {usernameError ? (
          <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800" role="alert">
            {usernameError}
          </p>
        ) : null}
        <div className="space-y-2">
          <label className="block text-sm font-medium text-neutral-700" htmlFor="register-password">密码</label>
          <input
            autoComplete="new-password"
            className={authInputClass}
            disabled={busy}
            id="register-password"
            onChange={(e) => setPassword(e.target.value)}
            type="password"
            value={password}
          />
          <p className="text-xs text-neutral-400">至少 {MIN_PASSWORD} 位。</p>
        </div>

        {error ? (
          <p className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700" role="alert">
            {error}
          </p>
        ) : null}

        <button
          className={authButtonClass}
          disabled={busy || !username || Boolean(usernameError) || password.length < MIN_PASSWORD}
          type="submit"
        >
          {busy ? "注册中…" : "注册"}
        </button>
      </form>
    </AuthShell>
  );
}
