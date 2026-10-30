"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { AuthShell, authButtonClass, authInputClass } from "@/components/auth/auth-shell";
import { loginLocal } from "@/core/api/auth";
import { authErrorMessage, suggestsRegistration } from "@/core/auth/error-messages";
import { markSessionActive, markSessionExpiry } from "@/core/auth/session-expiry";

/** 只允许跳回站内路径，别让 ?next= 变成开放重定向 */
function safeNext(raw: string | null): string {
  if (!raw) return "/workspace";
  if (!raw.startsWith("/") || raw.startsWith("//")) return "/workspace";
  return raw;
}

interface FormError {
  message: string;
  suggestRegister: boolean;
}

/**
 * 登录表单（M9 §2.12）。
 *
 * 登录标识只有**用户名**；邮箱不再参与认证。JWT 只进 HttpOnly cookie，
 * 响应体没有 token。失败原因按 code 逐条翻译，并在"账号不存在"时给出注册入口。
 */
export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const expired = params.get("reason") === "expired";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<FormError | null>(null);

  const submit = async () => {
    if (busy || !username || !password) return;
    setBusy(true);
    setError(null);
    try {
      const result = await loginLocal(username, password, rememberMe);
      markSessionExpiry(result.expires_in);
      markSessionActive();
      router.replace(next);
    } catch (cause) {
      setError({
        message: authErrorMessage(cause),
        suggestRegister: suggestsRegistration(cause),
      });
      setBusy(false);
    }
  };

  return (
    <AuthShell
      title="登录"
      description={expired ? "登录状态已过期，重新登录即可继续。" : "用你的用户名登录。"}
      footer={
        <>
          还没有账号？
          <Link className="ml-1 underline hover:text-neutral-600" href="/register">
            注册
          </Link>
        </>
      }
    >
      {expired ? (
        <p className="mb-4 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800" role="status">
          为了安全，登录凭证会定期失效。你之前的位置已经记下了。
        </p>
      ) : null}
      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="space-y-2">
          <label className="block text-sm font-medium text-neutral-700" htmlFor="login-username">
            用户名
          </label>
          <input
            autoComplete="username"
            className={authInputClass}
            disabled={busy}
            id="login-username"
            onChange={(e) => setUsername(e.target.value)}
            type="text"
            value={username}
          />
        </div>
        <div className="space-y-2">
          <label className="block text-sm font-medium text-neutral-700" htmlFor="login-password">密码</label>
          <input
            autoComplete="current-password"
            className={authInputClass}
            disabled={busy}
            id="login-password"
            onChange={(e) => setPassword(e.target.value)}
            type="password"
            value={password}
          />
        </div>
        <label className="flex items-center gap-2 text-sm text-neutral-600">
          <input
            checked={rememberMe}
            disabled={busy}
            onChange={(e) => setRememberMe(e.target.checked)}
            type="checkbox"
          />
          记住我
        </label>

        {error ? (
          <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700" role="alert">
            <p>{error.message}</p>
            {error.suggestRegister ? (
              <Link
                className="mt-1 inline-block underline hover:text-red-900"
                href={`/register?username=${encodeURIComponent(username)}`}
              >
                用这个用户名注册
              </Link>
            ) : null}
          </div>
        ) : null}

        <button className={authButtonClass} disabled={busy || !username || !password} type="submit">
          {busy ? "登录中…" : "登录"}
        </button>
      </form>
    </AuthShell>
  );
}
