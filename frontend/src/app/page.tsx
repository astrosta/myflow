import Link from "next/link";

import { HomeAuthLink } from "@/components/auth/home-auth-link";

/**
 * 落地页。页面本身静态——它存在的意义是"不加载聊天页的任何东西"，
 * 所以这里只挂一个很小的会话入口 island，不引用 markdown / 高亮 / SDK 代码（M5 §2）。
 *
 * 文案只写可验证的产品行为：过程可见、断点可续、能力可组装、边界可配置。
 * 每一条都能在 backend/README.md 或 chat 组件里找到对应实现，避免首页变成愿望清单。
 */

const FEATURES = [
  {
    title: "过程不是黑箱",
    body: "思考在答案之前展开，工具调用收成可展开的一行；子任务、上传文件、产物与引用各归其位。文本产物先取前 1 MiB，需要全文再显式加载；历史与实时流按服务端顺序合并，长会话也不会读出一条错位的时间线。",
    detail: "思考 · 工具调用 · 子任务 · 产物预览",
  },
  {
    title: "刷新不是重来",
    body: "页面刷新会重新接上正在跑的 run，从断点继续订阅；错过的流事件会明确提示并恢复。需要你决定时，它停下来等确认；取消只停止后续动作，已经拿到的结果不会消失。",
    detail: "断点续传 · 中断确认 · 取消保留",
  },
  {
    title: "能力装得进来",
    body: "知识库按文档类型切分与检索，回答带得回原文；技能把一套流程写成可扫描、可生效的 SOP；MCP 工具先检索再执行，模型只在需要时看见它们；长期记忆与用户画像让下一次不必从零解释。",
    detail: "知识库 · 技能 · MCP · 记忆",
  },
  {
    title: "边界守得住",
    body: "工具在本地、OpenSandbox 或 E2B 沙箱里执行；多用户隔离、会话鉴权、PII 脱敏与可观测性都可配置。模型接入、存储与运行环境都由你的部署决定。",
    detail: "沙箱 · 多用户 · PII · 可观测性",
  },
];

export default function LandingPage() {
  return (
    <div className="flex min-h-dvh flex-col bg-white">
      <header className="mx-auto flex w-full max-w-5xl items-center justify-between px-6 py-6">
        <Link className="text-sm font-semibold tracking-wide text-neutral-900" href="/">
          MyFlow
        </Link>
        <nav aria-label="主导航" className="flex items-center gap-6 text-sm text-neutral-500">
          <a className="transition-colors hover:text-neutral-900" href="#capabilities">
            能力
          </a>
          <Link className="transition-colors hover:text-neutral-900" href="/workspace">
            工作区
          </Link>
          <HomeAuthLink />
        </nav>
      </header>

      <main className="mx-auto w-full max-w-5xl flex-1 px-6">
        <section className="border-b border-neutral-200 pb-14 pt-10">
          <h1 className="max-w-3xl text-balance text-3xl font-semibold leading-tight tracking-tight text-neutral-900 sm:text-4xl">
            把 Agent 的长任务，放进一个读得完、接得住的工作区。
          </h1>
          <p className="mt-5 max-w-2xl text-base leading-relaxed text-neutral-600">
            MyFlow 是一个自托管的 Agent 工作区：把思考、工具调用、子任务、上传文件与产物放回同一条正确排序的时间线。
          </p>
          <p className="mt-3 max-w-2xl text-sm leading-relaxed text-neutral-500">
            刷新、断线或中断之后，会话仍能接着走；知识库、技能、MCP 与长期记忆按任务组装，不必换一套工具从头再来。
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-x-6 gap-y-3">
            <Link
              className="inline-flex h-10 items-center rounded-md bg-neutral-900 px-5 text-sm font-medium text-white transition-colors hover:bg-neutral-700"
              href="/workspace"
            >
              进入工作区
            </Link>
            <a
              className="text-sm font-medium text-neutral-600 underline-offset-4 transition-colors hover:text-neutral-900 hover:underline"
              href="#capabilities"
            >
              看看它如何工作
            </a>
          </div>
        </section>

        <section aria-labelledby="capabilities-title" className="py-14" id="capabilities">
          <h2
            className="max-w-3xl text-balance text-2xl font-semibold tracking-tight text-neutral-900"
            id="capabilities-title"
          >
            过程、连接、能力、边界，都在会话里。
          </h2>
          <div className="mt-10 grid gap-x-10 gap-y-9 sm:grid-cols-2">
            {FEATURES.map((feature) => (
              <article className="border-t border-neutral-200 pt-5" key={feature.title}>
                <h3 className="text-base font-semibold text-neutral-900">{feature.title}</h3>
                <p className="mt-2.5 max-w-prose text-sm leading-relaxed text-neutral-600">{feature.body}</p>
                <p className="mt-4 text-[11px] font-medium tracking-wide text-neutral-500">{feature.detail}</p>
              </article>
            ))}
          </div>
        </section>

        <section aria-label="产品原则" className="border-t border-neutral-200 py-10">
          <p className="max-w-3xl text-balance text-base leading-relaxed text-neutral-700">
            MyFlow 对长任务的底线很简单：要么给你一个结果，要么明确告诉你为什么没有——不留永远转圈的卡片，也不假装成功。
          </p>
        </section>
      </main>

      <footer className="mx-auto w-full max-w-5xl px-6 py-8 text-xs text-neutral-500">
        MyFlow · 自托管 Agent 工作区 · LangGraph Platform 兼容网关
      </footer>
    </div>
  );
}
