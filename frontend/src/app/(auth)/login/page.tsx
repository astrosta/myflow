import { Suspense } from "react";

import { LoginForm } from "./login-form";

export const metadata = {
  title: "登录",
};

/**
 * 登录页。
 *
 * 表单在 Suspense 里：它要读 ?reason / ?next（useSearchParams），
 * 而 useSearchParams 在预渲染时必须被 Suspense 包住，否则整个路由会退化成
 * 动态渲染——登录页变成按请求现算，没必要。
 */
export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}
