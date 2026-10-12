import Link from "next/link";

/**
 * 落地页。静态、无数据依赖——它存在的意义是"不加载聊天页的任何东西"，
 * 所以这里不引用任何 markdown / 高亮 / SDK 代码（M5 §2）。
 */

const FEATURES = [
  {
    title: "工具调用是可见的",
    body: "每一次工具调用都会渲染成卡片：工具名、参数、状态、结果一屏看全，不用去翻日志。",
  },
  {
    title: "刷新不重跑",
    body: "页面刷新时自动 join 正在进行的 run，从断点继续订阅；连接断了会明确提示正在恢复。",
  },
  {
    title: "产物只读预览",
    body: "文本产物走 HTTP 范围请求，先取前 1 MiB；需要全文时再显式加载，不预挂重型编辑器。",
  },
];

export default function LandingPage() {
  return (
    <div className="flex min-h-dvh flex-col bg-white">
      <header className="mx-auto flex w-full max-w-5xl items-center justify-between px-6 py-6">
        <span className="text-sm font-semibold tracking-wide text-neutral-900">MyFlow</span>
        <nav className="flex items-center gap-6 text-sm text-neutral-500">
          <Link className="transition-colors hover:text-neutral-900" href="/workspace">
            工作区
          </Link>
          <Link className="transition-colors hover:text-neutral-900" href="/login">
            登录
          </Link>
        </nav>
      </header>

      <main className="mx-auto w-full max-w-5xl flex-1 px-6">
        <section className="border-b border-neutral-200 pb-14 pt-10">
          <p className="text-xs font-medium uppercase tracking-[0.18em] text-neutral-400">
            Agent Workspace
          </p>
          <h1 className="mt-4 max-w-3xl text-3xl font-semibold leading-tight tracking-tight text-neutral-900 sm:text-4xl">
            把一条长任务的 Agent 对话，做成能安心读完的工作区
          </h1>
          <p className="mt-5 max-w-2xl text-base leading-relaxed text-neutral-600">
            历史与实时流合并成一条顺序正确的对话；思考在答案之上，工具参数和结果都留在卡片里；
            中断看得见，恢复也看得见。
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <Link
              className="inline-flex h-10 items-center rounded-md bg-neutral-900 px-5 text-sm font-medium text-white transition-colors hover:bg-neutral-700"
              href="/workspace"
            >
              进入工作区
            </Link>
          </div>
        </section>

        <section className="grid gap-6 py-14 sm:grid-cols-3">
          {FEATURES.map((feature) => (
            <article key={feature.title} className="rounded-lg border border-neutral-200 p-5">
              <h2 className="text-sm font-semibold text-neutral-900">{feature.title}</h2>
              <p className="mt-3 text-sm leading-relaxed text-neutral-600">{feature.body}</p>
            </article>
          ))}
        </section>
      </main>

      <footer className="mx-auto w-full max-w-5xl px-6 py-8 text-xs text-neutral-400">
        MyFlow · LangGraph Platform 兼容网关的前端
      </footer>
    </div>
  );
}
