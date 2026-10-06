import Link from "next/link";

export const metadata = {
  title: "登录",
};

/**
 * 占位登录页（M9 才接真实认证）。
 *
 * 这里**故意不读 cookie、不读会话**：认证一旦进来，多半要把这个路由换成
 * 动态渲染，那是 M9 的决定；现在提前读 cookie 只会让根边界先破一次。
 */
export default function LoginPage() {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-neutral-50 px-6">
      <div className="w-full max-w-sm rounded-xl border border-neutral-200 bg-white p-8">
        <h1 className="text-lg font-semibold text-neutral-900">登录</h1>
        <p className="mt-2 text-sm leading-relaxed text-neutral-500">
          认证在 M9 接入。当前所有请求都走同源网关，没有会话态需要伪造。
        </p>

        <form className="mt-6 space-y-4" aria-describedby="login-note">
          <div className="space-y-2">
            <label className="block text-sm font-medium text-neutral-700" htmlFor="login-email">
              邮箱
            </label>
            <input
              className="h-10 w-full rounded-md border border-neutral-300 bg-neutral-50 px-3 text-sm text-neutral-500"
              disabled
              id="login-email"
              placeholder="尚未接入"
              type="email"
            />
          </div>
          <div className="space-y-2">
            <label className="block text-sm font-medium text-neutral-700" htmlFor="login-password">
              密码
            </label>
            <input
              className="h-10 w-full rounded-md border border-neutral-300 bg-neutral-50 px-3 text-sm text-neutral-500"
              disabled
              id="login-password"
              placeholder="尚未接入"
              type="password"
            />
          </div>
          <button
            className="h-10 w-full cursor-not-allowed rounded-md bg-neutral-200 text-sm font-medium text-neutral-500"
            disabled
            type="button"
          >
            登录（M9 开放）
          </button>
        </form>

        <p className="mt-4 text-xs text-neutral-400" id="login-note">
          现在可以先<Link className="ml-1 underline hover:text-neutral-600" href="/workspace">进入工作区</Link>
          。
        </p>
      </div>
    </div>
  );
}
