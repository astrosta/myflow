import Link from "next/link";

import { BookOpenIcon, MemoryIcon, ToolIcon, UserIcon, WrenchIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import type { ThreadSummary } from "@/core/types";
import { cn, formatTimestamp } from "@/lib/utils";

/**
 * 会话列表（纯展示：数据由外层容器传进来）。
 *
 * 侧栏不自己取数，是因为"列表怎么来的"（分页、失效、乐观插入）属于
 * 服务端状态，应该由 TanStack Query 在容器里管；这里只负责画出来。
 *
 * 版面从上到下：品牌行 → 新对话 → 功能导航 → **最近对话**（历史会话）。
 * 功能导航固定为：我的画像 → 记忆管理 → 技能中心 → 工具管理 → 知识库。
 * 历史会话挂在一个带标题的分区下面，而不是直接铺在导航里——它和上面两个
 * 是不同性质的东西：上面是"去哪"，下面是"回过哪"，中间没有分隔就读成一串
 * 同级的按钮。标题始终渲染（空列表也一样），免得列表加载完再冒出一行字、
 * 把下面的会话整体往下推。
 */
const NEW_CHAT_CLASS =
  "flex h-9 w-full items-center justify-center rounded-md border border-neutral-300 bg-white text-xs font-medium text-neutral-700 transition-colors hover:border-neutral-400 hover:text-neutral-900";

/**
 * 导航项（我的画像 / 记忆管理 / 技能中心 / 工具管理 / 知识库）共用一套形制。
 *
 * 四个入口是同一类东西——"去哪"，形状就得一致；各写一份 class 的话，
 * 新增入口时高亮态迟早会跟已有入口不一样。
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
  /** 当前是否在工具管理路由 */
  toolsActive?: boolean;
  /** 当前是否在「我的画像」 */
  profileActive?: boolean;
  /** 当前是否在「记忆管理」 */
  memoryActive?: boolean;
  /**
   * 点了某个会话标题。
   *
   * **不拦导航**：链接照常把地址换成目标会话；这个回调只用来通知聊天区
   * "用户明确选了一个会话，把消息拉到最新一条"。点当前会话时地址不变，
   * 回调就是让列表回到底部的唯一途径。
   */
  onThreadClick?: (threadId: string) => void;
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
  toolsActive = false,
  profileActive = false,
  memoryActive = false,
  onThreadClick,
  className,
}: SidebarProps) {
  return (
    <aside
      aria-label="工作区导航"
      className={cn(
        /**
         * **只有下面的「最近对话」nav 可以滚，整个侧栏不能滚。**
         *
         * 侧栏自己也 overflow-y-auto 的话，滚标题列表滚到底后会链式带动
         * 整个侧栏（品牌行、导航、标题一起动），看起来像"最近会话跟着
         * title 列表在跑"。这里用 overflow-hidden 把外层锁死，nav 再用
         * overscroll-contain 截断滚动链。
         */
        "flex h-full min-h-0 w-64 shrink-0 flex-col overflow-hidden border-r border-neutral-200 bg-neutral-50",
        className,
      )}
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
          aria-current={profileActive ? "page" : undefined}
          className={navItemClass(profileActive)}
          href="/workspace/profile"
        >
          <UserIcon className="shrink-0" size={15} />
          我的画像
        </Link>
        <Link
          aria-current={memoryActive ? "page" : undefined}
          className={navItemClass(memoryActive)}
          href="/workspace/memory"
        >
          <MemoryIcon className="shrink-0" size={15} />
          记忆管理
        </Link>
        <Link
          aria-current={skillsActive ? "page" : undefined}
          className={navItemClass(skillsActive)}
          href="/workspace/skills"
        >
          <ToolIcon className="shrink-0" size={15} />
          技能中心
        </Link>
        <Link
          aria-current={toolsActive ? "page" : undefined}
          className={navItemClass(toolsActive)}
          href="/workspace/tools"
        >
          <WrenchIcon className="shrink-0" size={15} />
          工具管理
        </Link>
        <Link
          aria-current={knowledgeActive ? "page" : undefined}
          className={navItemClass(knowledgeActive)}
          href="/workspace/knowledge"
        >
          <BookOpenIcon className="shrink-0" size={15} />
          知识库
        </Link>
      </nav>

      <div className="flex min-h-0 flex-1 flex-col">
        {/**
         * 标题**放在滚动区外面**，不是 sticky 在里面。
         *
         * 这样"最近对话"和条数从一开始就不参与滚动：往下翻 title 列表时，
         * 它不会跟着列表跑。结构上保证，比算 sticky 的 top/背景更稳。
         */}
        <h2
          className="flex shrink-0 items-baseline gap-1.5 px-4 pb-1.5 text-[11px] font-medium text-neutral-400"
          id="recent-threads-heading"
        >
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

        <nav
          aria-labelledby="recent-threads-heading"
          className="thin-scrollbar min-h-0 flex-1 touch-pan-y overflow-y-auto overscroll-contain px-2 pb-3"
        >
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
                    onClick={() => onThreadClick?.(thread.thread_id)}
                  >
                    <span className="block truncate text-xs">{title}</span>
                    <span className="block text-[10px] text-neutral-400">{formatTimestamp(thread.updated_at)}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>
      </div>
    </aside>
  );
}