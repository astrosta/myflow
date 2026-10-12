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

/** 失败时给用户一条出路：能说清"该去注册"就把他送过去 */
interface FormError {
  message: string;
  suggestRegister: boolean;
}

/**
 * 登录表单（M9 §2.12）。
 *
 * 五条和后端约定绑死的细节：
 *
 * 1. **没有任何"存 token"的代码。** JWT 只在 HttpOnly cookie 里（§2.4），
 *    响应体里根本没有 token 字符串；登录成功之后浏览器自己带 cookie。
 * 2. remember_me 只影响 cookie 的 max_age，密码从不落盘（§2.4）。
 * 3. 失败原因**按 code 逐条翻译**（见 core/auth/error-messages）：默认部署会区分
 *    "没这个账号"和"密码错"，所以文案也区分，并给出下一步（去注册 / 重试）。
 *    登录只用**邮箱**：用户名是由邮箱派生出来的内部标识，界面上不再暴露它，
 *    "去注册"链接固定预填邮箱。
 *    `auth.local.specific_errors=false` 的部署只会给 invalid_credentials，
 *    那时文案自动退回"邮箱或密码不对"——不会替用户断言"你没注册过"。
 * 4. `expires_in` 要记下来：AuthGuard 靠它主动跳转，而不是等某个请求 401。
 * 5. **登录失败的 401 不是"会话失效"**：那条广播在前端就被排除掉了
 *    （core/api/unauthorized 的 isAuthEndpoint），否则用户点一次登录就会被弹走。
 */
export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const expired = params.get("reason") === "expired";

  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  // "去注册"那条链接带上用户刚敲的邮箱，省得他再打一遍
  const [rememberMe, setRememberMe] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<FormError | null>(null);

  const submit = async () => {
    if (busy || !identifier || !password) return;
    setBusy(true);
    setError(null);
    try {
      const result = await loginLocal(identifier, password, rememberMe);
      markSessionExpiry(result.expires_in);
      markSessionActive();
      // 系统只有普通用户，没有"首次建管理员"这一步，也就没有 needs_setup 的分叉：
      // 登录成功就是去原来的位置（或工作区）
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
        description={expired ? "登录状态已过期，重新登录即可继续。" : "用你的邮箱登录。"}
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
          <label className="block text-sm font-medium text-neutral-700" htmlFor="login-identifier">
            邮箱
          </label>
          <input
            autoComplete="username"
            className={authInputClass}
            disabled={busy}
            id="login-identifier"
            onChange={(e) => setIdentifier(e.target.value)}
            type="email"
            value={identifier}
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
                href={`/register?email=${encodeURIComponent(identifier)}`}
              >
                用这个邮箱注册
              </Link>
            ) : null}
          </div>
        ) : null}

        <button className={authButtonClass} disabled={busy || !identifier || !password} type="submit">
          {busy ? "登录中…" : "登录"}
        </button>
      </form>
    </AuthShell>
  );
}
