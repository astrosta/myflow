import Link from "next/link";

import { BookOpenIcon, SettingsIcon, ToolIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import type { ThreadSummary } from "@/core/types";
import { cn, formatTimestamp } from "@/lib/utils";

/**
 * 会话列表（纯展示：数据由外层容器传进来）。
 *
 * 侧栏不自己取数，是因为"列表怎么来的"（分页、失效、乐观插入）属于
 * 服务端状态，应该由 TanStack Query 在容器里管；这里只负责画出来。
 *
 * 版面从上到下：品牌行 → 新对话 → 知识库 → **最近对话**（历史会话）。
 * 历史会话挂在一个带标题的分区下面，而不是直接铺在导航里——它和上面两个
 * 是不同性质的东西：上面是"去哪"，下面是"回过哪"，中间没有分隔就读成一串
 * 同级的按钮。标题始终渲染（空列表也一样），免得列表加载完再冒出一行字、
 * 把下面的会话整体往下推。
 */
const NEW_CHAT_CLASS =
  "flex h-9 w-full items-center justify-center rounded-md border border-neutral-300 bg-white text-xs font-medium text-neutral-700 transition-colors hover:border-neutral-400 hover:text-neutral-900";

/**
 * 导航项（知识库 / 技能 / 审核）共用一套形制。
 *
 * 三个入口是同一类东西——"去哪"，形状就得一致；各写一份 class 的话，
 * 加第三个入口时高亮态迟早会跟上两个不一样。
 */
function navItemClass(active: boolean): string {
  return cn(
    "flex h-9 w-full items-center gap-2 rounded-md border px-3 text-xs font-medium transition-colors",
    active
      ? "border-neutral-300 bg-white text-neutral-900 shadow-sm"
      : "border-transparent text-neutral-600 hover:bg-white/70 hover:text-neutral-900",
  );
}

export interface SidebarProps {
  threads: readonly ThreadSummary[];
  activeThreadId?: string | null;
  loading?: boolean;
  error?: string | null;
  onRefresh?: () => void;
  /**
   * "新对话"。传了才渲染成按钮。
   *
   * 不传就退回 <Link href="/workspace">：地址是 history.replaceState 换的，
   * Next 的路由树仍停在 /workspace，那个 Link 在新会话里会被判定成
   * "已经在这儿"而空转 —— 所以真实调用点必须走这个回调。
   */
  onNewChat?: () => void;
  /** 当前是否在知识库路由；入口高亮用 */
  knowledgeActive?: boolean;
  /** 当前是否在技能路由（/workspace/skills 及其子路由） */
  skillsActive?: boolean;
  /** 当前是否在审核路由（/workspace/admin/skills） */
  reviewActive?: boolean;
  /**
   * 当前会话是不是管理员；只决定要不要画「审核」入口。
   *
   * **这是展示层的显隐，不是权限闸门。** 真正的闸门在后端 /admin 路由的
   * admin scope 上：普通用户直接访问 /workspace/admin/skills 会拿到 403。
   * 藏起来只是"别给一个必然失败的链接"，任何客户端判断都不能替代服务端。
   */
  isAdmin?: boolean;
  className?: string;
}

export function Sidebar({
  threads,
  activeThreadId = null,
  loading = false,
  error = null,
  onRefresh,
  onNewChat,
  knowledgeActive = false,
  skillsActive = false,
  reviewActive = false,
  isAdmin = false,
  className,
}: SidebarProps) {
  return (
    <aside
      aria-label="工作区导航"
      className={cn("flex h-full w-64 shrink-0 flex-col border-r border-neutral-200 bg-neutral-50", className)}
    >
      <div className="flex items-center gap-2 px-3 py-3">
        <Link className="text-sm font-semibold text-neutral-900" href="/">
          MyFlow
        </Link>
        {onRefresh ? (
          <button
            className="ml-auto rounded px-2 py-1 text-[11px] text-neutral-500 transition-colors hover:bg-neutral-200/60 hover:text-neutral-800"
            onClick={onRefresh}
            type="button"
          >
            刷新
          </button>
        ) : null}
      </div>

      <div className="px-3 pb-3">
        {onNewChat ? (
          <button className={NEW_CHAT_CLASS} onClick={onNewChat} type="button">
            ＋ 新对话
          </button>
        ) : (
          <Link className={NEW_CHAT_CLASS} href="/workspace">
            ＋ 新对话
          </Link>
        )}
      </div>

      <nav aria-label="功能导航" className="space-y-1 px-3 pb-3">
        <Link
          aria-current={knowledgeActive ? "page" : undefined}
          className={navItemClass(knowledgeActive)}
          href="/workspace/knowledge"
        >
          <BookOpenIcon className="shrink-0" size={15} />
          知识库
        </Link>
        <Link
          aria-current={skillsActive ? "page" : undefined}
          className={navItemClass(skillsActive)}
          href="/workspace/skills"
        >
          <ToolIcon className="shrink-0" size={15} />
          技能
        </Link>
        {/**
         * 「审核」只对管理员画出来：普通用户看不到这个链接。
         * 再次强调——藏起来不等于挡住，闸门在后端 admin scope。
         */}
        {isAdmin ? (
          <Link
            aria-current={reviewActive ? "page" : undefined}
            className={navItemClass(reviewActive)}
            href="/workspace/admin/skills"
          >
            <SettingsIcon className="shrink-0" size={15} />
            审核
          </Link>
        ) : null}
      </nav>

      <nav aria-label="最近对话" className="thin-scrollbar min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        <h2 className="flex items-baseline gap-1.5 px-2 pb-1.5 text-[11px] font-medium text-neutral-400">
          最近对话
          {/**
           * 条数用 aria-hidden 挂在标题后面：读屏念一遍导航名就够了，
           * "最近对话 12" 只是给眼睛看的密度提示。
           */}
          {threads.length ? (
            <span aria-hidden className="text-neutral-300">
              {threads.length}
            </span>
          ) : null}
        </h2>

        {loading && threads.length === 0 ? (
          <div className="space-y-2 px-1" aria-hidden>
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
            <Skeleton className="h-9 w-full" />
          </div>
        ) : null}

        {error ? (
          <p className="px-2 py-1 text-[11px] leading-relaxed text-red-600" role="alert">
            {error}
          </p>
        ) : null}

        {!loading && !error && threads.length === 0 ? (
          <p className="px-2 py-2 text-xs leading-relaxed text-neutral-400">还没有会话，点上面的「新对话」开始。</p>
        ) : null}

        <ul className="space-y-0.5">
          {threads.map((thread) => {
            const active = thread.thread_id === activeThreadId;
            const title = thread.display_name?.trim() ? thread.display_name : "新对话";
            return (
              <li key={thread.thread_id}>
                <Link
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "block rounded-md px-2 py-1.5 transition-colors",
                    active ? "bg-white text-neutral-900 shadow-sm" : "text-neutral-600 hover:bg-white/70 hover:text-neutral-900",
                  )}
                  href={"/workspace/chats/" + thread.thread_id}
                >
                  <span className="block truncate text-xs">{title}</span>
                  <span className="block text-[10px] text-neutral-400">{formatTimestamp(thread.updated_at)}</span>
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>
    </aside>
  );
}
