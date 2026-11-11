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
    <div className="flex min-h-dvh flex-col bg-white">
      <header className="mx-auto flex w-full max-w-6xl items-center justify-between px-6 py-6">
        <Link className="text-sm font-semibold tracking-wide text-neutral-900" href="/">
          MyFlow
        </Link>
        <nav aria-label="主导航" className="flex items-center gap-6 text-sm text-neutral-500">
          <Link className="transition-colors hover:text-neutral-900" href="/workspace">
            工作区
          </Link>
          <HomeAuthLink />
        </nav>
      </header>

      <main className="mx-auto w-full max-w-6xl flex-1 px-6">
        <section className="grid gap-10 border-b border-neutral-200 pb-14 pt-12 lg:grid-cols-[minmax(0,1.55fr)_minmax(18rem,0.75fr)] lg:items-start lg:gap-16 lg:pb-20 lg:pt-16">
          <h1 className="max-w-3xl text-4xl font-semibold leading-[1.15] tracking-[-0.035em] text-neutral-900 sm:text-5xl lg:text-6xl">
            <span className="block text-balance">越用越懂你的智能体</span>
            <span className="mt-4 block text-pretty text-lg font-normal leading-[1.7] tracking-normal text-neutral-500 sm:mt-5 sm:text-xl">
              <span className="text-neutral-300">——</span>
              它记得你们的每一次协作，并不断把经验转化为更贴合你的能力。
            </span>
          </h1>

          <div className="max-w-md lg:pt-2">
            <p className="text-base leading-relaxed text-neutral-600">
              MyFlow 是自托管 Agent 工作区：每次协作，都为下一次留下可用的能力。
            </p>
            <div className="mt-7 flex flex-wrap items-center gap-x-6 gap-y-3">
              <Link
                className="inline-flex h-10 items-center rounded-md bg-neutral-900 px-5 text-sm font-medium text-white transition-colors hover:bg-neutral-700"
                href="/workspace"
              >
                进入工作区
              </Link>
              <a
                className="text-sm font-medium text-neutral-500 underline-offset-4 transition-colors hover:text-neutral-900 hover:underline"
                href="#capabilities"
              >
                查看五种能力
              </a>
            </div>
          </div>
        </section>

        <section
          aria-labelledby="capabilities-title"
          className="py-14 sm:py-16"
          id="capabilities"
        >
          <div className="max-w-2xl">
            <h2
              className="text-2xl font-semibold tracking-tight text-neutral-900 sm:text-3xl"
              id="capabilities-title"
            >
              从记得你，到更懂你。
            </h2>
            <p className="mt-3 text-sm leading-relaxed text-neutral-500">
              记忆、技能、工具、知识与评测在同一个工作区里彼此接续；每一次协作，都能留下下一次可复用的经验。
            </p>
          </div>

          <div className="mt-9 grid gap-px border border-neutral-200 bg-neutral-200 sm:grid-cols-2 lg:grid-cols-6">
            {FEATURES.map((feature, index) => (
              <article
                className={[
                  "flex flex-col bg-white p-6 sm:min-h-48 sm:p-7",
                  index < 3 ? "lg:col-span-2" : "lg:col-span-3",
                  index === FEATURES.length - 1 ? "sm:col-span-2" : "",
                ].join(" ")}
                key={feature.title}
              >
                <feature.Icon className="text-neutral-400" size={20} />
                <h3 className="mt-5 text-base font-semibold tracking-tight text-neutral-900">
                  {feature.title}
                </h3>
                <p className="mt-2.5 max-w-prose text-sm leading-7 text-neutral-600">
                  {feature.body}
                </p>
              </article>
            ))}
          </div>
        </section>

        <section aria-label="产品原则" className="border-t border-neutral-200 py-10">
          <p className="max-w-3xl text-balance text-base leading-relaxed text-neutral-700">
            MyFlow 的底线很简单：要么给你一个结果，要么明确告诉你为什么没有——不留永远转圈的卡片，也不假装成功。
          </p>
        </section>
      </main>

      <footer className="mx-auto w-full max-w-6xl px-6 py-8 text-xs text-neutral-500">
        MyFlow · 自托管 Agent 工作区 · LangGraph Platform 兼容网关
      </footer>
    </div>
  );
}
