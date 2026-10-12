"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { AuthShell, authButtonClass, authInputClass } from "@/components/auth/auth-shell";
import { register } from "@/core/api/auth";
import { authErrorMessage } from "@/core/auth/error-messages";
import { markSessionActive } from "@/core/auth/session-expiry";
import { usernameFromEmail, validateUsername } from "@/core/auth/username";

/** 后端 RegisterRequest 的密码下限（M9 §2.12） */
const MIN_PASSWORD = 8;

/** 用户名的形状由后端定死（§2.4）；规则镜像在 core/auth/username.ts */

/**
 * 自助注册（M9 §2.12）。
 *
 * 由 `auth.allow_registration` 控制：关掉时后端返回 403 registration_closed，
 * 这里把那个 code 翻成一句人话（见 core/auth/error-messages）。
 * 前端**不做**"先查能不能注册再决定显不显示表单"——那要多一次请求，
 * 而且开关是热重载的，查完也可能已经变了。让它失败一次更诚实。
 *
 * 邮箱与用户名**都要**（§2.4）：邮箱是登录标识与找回入口，用户名是第二个登录标识、
 * 也是给人看的名字。从登录页过来时带着 `?email=`（"这个账号没注册"那条提示），
 * 预填进输入框——让用户重敲一遍刚打过的东西是没有意义的摩擦。
 *
 * 注册成功会直接种下 cookie（后端返回 201 + Set-Cookie），所以不用再登录一次。
 */
export function RegisterForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [email, setEmail] = useState(params.get("email") ?? "");
  const [password, setPassword] = useState("");

  /**
   * 用户名不再让用户填，**直接从邮箱名称派生**（@ 前面那段）。
   *
   * 它是**实时算出来的**，不是一次性的初始值：用户改邮箱，预览跟着变，
   * 提交时用的也是同一个函数，不会出现"预览和实际发出去的不一致"。
   *
   * 只做归一（去空白、转小写），不替用户"修好"非法字符——猜出来的名字
   * 和用户以为的不是一回事，而且可能恰好撞上别人的账号。
   */
  const username = usernameFromEmail(email);
  const usernameError = username ? validateUsername(username) : null;
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (busy || !email || password.length < MIN_PASSWORD) return;
    if (usernameError) {
      setError(usernameError);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await register(email, username, password);
      markSessionActive();
      // 注册响应体是 UserResponse，没有 expires_in；用服务端默认 TTL 之外
      // 我们只能等被动检测。所以这里不 markSessionExpiry。
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
          <label className="block text-sm font-medium text-neutral-700" htmlFor="register-email">邮箱</label>
          <input
            autoComplete="username"
            className={authInputClass}
            disabled={busy}
            id="register-email"
            onChange={(e) => setEmail(e.target.value)}
            type="email"
            value={email}
          />
        </div>
        {/**
          * 用户名**不显示**：它由邮箱自动派生，用户不需要知道也不用选。
          *
          * 派生和校验仍然在（见上面的 usernameFromEmail / validateUsername）——
          * 不校验的话会拿一个非法名字去提交，换回一个 422。
          */}
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
          disabled={busy || !email || Boolean(usernameError) || password.length < MIN_PASSWORD}
          type="submit"
        >
          {busy ? "注册中…" : "注册"}
        </button>
      </form>
    </AuthShell>
  );
}
