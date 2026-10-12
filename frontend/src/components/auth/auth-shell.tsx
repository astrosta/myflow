"use client";

import type { ReactNode } from "react";

/** 登录 / 初始化共用的卡片外壳，保证两个页面观感一致 */
export function AuthShell({
  title,
  description,
  children,
  footer,
}: {
  title: string;
  description: string;
  children: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-neutral-50 px-6">
      <div className="w-full max-w-sm rounded-xl border border-neutral-200 bg-white p-8">
        <h1 className="text-lg font-semibold text-neutral-900">{title}</h1>
        <p className="mt-2 text-sm leading-relaxed text-neutral-500">{description}</p>
        <div className="mt-6">{children}</div>
        {footer ? <div className="mt-4 text-xs text-neutral-400">{footer}</div> : null}
      </div>
    </div>
  );
}

export const authInputClass =
  "h-10 w-full rounded-md border border-neutral-300 bg-white px-3 text-sm text-neutral-900 placeholder:text-neutral-400 focus:border-neutral-500 focus:outline-none disabled:bg-neutral-50";

export const authButtonClass =
  "h-10 w-full rounded-md bg-neutral-900 text-sm font-medium text-white transition-colors hover:bg-neutral-700 disabled:cursor-not-allowed disabled:bg-neutral-300";
