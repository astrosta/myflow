import Link from "next/link";

import { HomeAuthLink } from "@/components/auth/home-auth-link";
import {
  ActivityIcon,
  BookOpenIcon,
  LibraryIcon,
  MemoryIcon,
  WrenchIcon,
} from "@/components/ui/icons";

/**
 * 落地页。页面本身静态——它存在的意义是"不加载聊天页的任何东西"，
 * 所以这里只挂一个很小的会话入口 island，不引用 markdown / 高亮 / SDK 代码（M5 §2）。
 *
 * 文案只写可验证的产品行为：记忆会积累、技能可启用、工具可检索、知识可溯源、运行可评测。
 * 每一条都能在 backend/README.md 或对应 workspace 页面里找到实现，避免首页变成愿望清单。
 */

const FEATURES = [
  {
    title: "记忆管理",
    body: "自动记住你的偏好、事实与决定，下一次对话不用再把背景从头讲一遍。",
    Icon: MemoryIcon,
  },
  {
    title: "技能中心",
    body: "把一套常用流程写成可扫描、可启用的技能，同类任务从重新解释变成一次调用。",
    Icon: BookOpenIcon,
  },
  {
    title: "工具管理",
    body: "接入并管理自己的 MCP 工具，模型先检索、再按需调用。",
    Icon: WrenchIcon,
  },
  {
    title: "知识库",
    body: "按文档类型切分与检索你的资料，回答能带着原文出处一起回来。",
    Icon: LibraryIcon,
  },
  {
    title: "可评测、可观测",
    body: "Agent 的运行轨迹可复查、评测结果可量化，变好还是变差都有据可查。",
    Icon: ActivityIcon,
  },
];

export default function LandingPage() {
  return (
    <div className="flex min-h-dvh flex-col bg-white selection:bg-neutral-200 selection:text-neutral-900">
      <a
        className="sr-only focus:not-sr-only focus:fixed focus:left-6 focus:top-6 focus:z-50 focus:rounded-md focus:bg-neutral-900 focus:px-4 focus:py-3 focus:text-sm focus:font-medium focus:text-white"
        href="#main-content"
      >
        跳到主要内容
      </a>

      <header className="mx-auto flex w-full max-w-6xl items-center justify-between px-6 py-3">
        <Link
          className="inline-flex min-h-11 items-center text-base font-semibold tracking-tight text-neutral-900"
          href="/"
        >
          MyFlow
        </Link>
        <nav aria-label="主导航" className="flex items-center gap-6 text-sm text-neutral-500">
          <Link
            className="inline-flex min-h-11 items-center transition-colors hover:text-neutral-900"
            href="/workspace"
          >
            工作区
          </Link>
          <HomeAuthLink />
        </nav>
      </header>

      <main
        className="mx-auto w-full max-w-6xl flex-1 px-6 focus:outline-none"
        id="main-content"
        tabIndex={-1}
      >
        <section className="pb-10 pt-10">
          <h1 className="max-w-3xl text-balance text-3xl font-semibold leading-tight tracking-tight text-neutral-900 sm:text-4xl">
            越用越懂你的智能体——它记得你们的每一次协作，并不断把经验转化为更贴合你的能力。
          </h1>
          <div className="mt-8">
            <Link
              className="inline-flex h-11 items-center rounded-md bg-neutral-900 px-6 text-sm font-medium text-white transition-colors hover:bg-neutral-700 active:bg-neutral-800"
              href="/workspace"
            >
              进入工作区
            </Link>
          </div>
        </section>

        <section aria-label="工作区能力" className="pb-14 pt-10">
          <div className="grid gap-px border border-neutral-200 bg-neutral-200 sm:grid-cols-2 lg:grid-cols-6">
            {FEATURES.map((feature, index) => (
              <article
                className={[
                  "flex flex-col bg-white p-6 sm:min-h-48 sm:p-7",
                  index < 3 ? "lg:col-span-2" : "lg:col-span-3",
                  index === FEATURES.length - 1 ? "sm:col-span-2" : "",
                ].join(" ")}
                key={feature.title}
              >
                <feature.Icon className="text-neutral-500" size={20} />
                <h2 className="mt-5 text-base font-semibold tracking-tight text-neutral-900">
                  {feature.title}
                </h2>
                <p className="mt-2.5 max-w-prose text-pretty text-sm leading-7 text-neutral-600">
                  {feature.body}
                </p>
              </article>
            ))}
          </div>
        </section>
      </main>

      <footer className="mx-auto w-full max-w-6xl px-6 py-6 text-xs text-neutral-500">
        MyFlow · 自托管 Agent 工作区 · LangGraph Platform 兼容网关
      </footer>
    </div>
  );
}
