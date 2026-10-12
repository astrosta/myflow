import { Suspense } from "react";

import { RegisterForm } from "./register-form";

export const metadata = {
  title: "注册",
};

/**
 * 注册页。
 *
 * 表单在 Suspense 里：它要读 `?email=`（useSearchParams），而 useSearchParams
 * 在预渲染时必须被 Suspense 包住，否则整个路由会退化成动态渲染（和登录页同一个理由）。
 */
export default function RegisterPage() {
  return (
    <Suspense fallback={null}>
      <RegisterForm />
    </Suspense>
  );
}
