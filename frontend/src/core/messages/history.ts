import type { ChatMessage } from "../types";

/**
 * 已加载历史页的管理（M5 §4.4）。
 *
 * **历史缓存失效时保留已加载的页。** 否则已经建立的排序位置会被丢掉，
 * 用户会看到列表重新洗牌一次。
 */

export interface MessagePage {
  /** 升序 */
  messages: ChatMessage[];
  /** 这个页之前还有没有更旧的消息 */
  hasMoreBefore: boolean;
  /** 取更旧一页时带的游标（`before_seq`） */
  nextBeforeSeq: number | null;
}

export interface HistoryWindow {
  /** `pages[0]` 是最新的一页，往后越旧 */
  pages: MessagePage[];
}

export function createHistoryWindow(): HistoryWindow {
  return { pages: [] };
}

const seqOf = (message: ChatMessage): number | null =>
  typeof message.seq === "number" && Number.isFinite(message.seq) ? message.seq : null;

/** 升序、按 seq 去重。服务端返回已经是升序的，这里只做防御 */
function normalizePage(messages: readonly ChatMessage[]): ChatMessage[] {
  const bySeq = new Map<number, ChatMessage>();
  const withoutSeq: ChatMessage[] = [];
  for (const message of messages) {
    const seq = seqOf(message);
    if (seq === null) withoutSeq.push(message);
    else bySeq.set(seq, message);
  }
  return [...bySeq.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([, message]) => message)
    .concat(withoutSeq);
}

/** 失效后重取第一页：**保留已经加载的更旧页** */
export function replaceNewestPage(
  window: HistoryWindow,
  page: MessagePage,
): HistoryWindow {
  const newest: MessagePage = { ...page, messages: normalizePage(page.messages) };
  const older = window.pages.slice(1);
  // 新的第一页可能已经覆盖到某些更旧页的范围（比如这一轮又写了很多消息），
  // 把完全重合的旧页丢掉，避免同一批消息出现两遍
  const freshSeqs = newest.messages.map(seqOf).filter((seq): seq is number => seq !== null);
  const floor = freshSeqs.length ? Math.min(...freshSeqs) : null;
  const kept =
    floor === null
      ? older
      : older.filter((candidate) =>
          candidate.messages.some((message) => {
            const seq = seqOf(message);
            return seq !== null && seq < floor;
          }),
        );

  return { pages: [newest, ...kept] };
}

/** 翻页：插入更旧的一页 */
export function prependOlderPage(
  window: HistoryWindow,
  page: MessagePage,
): HistoryWindow {
  const normalized: MessagePage = { ...page, messages: normalizePage(page.messages) };
  if (!normalized.messages.length) return window;

  // 已经加载过的范围不重复插入：这一页要是没有带来更旧的消息，就丢
  const loadedSeqs = loadedMessages(window)
    .map(seqOf)
    .filter((seq): seq is number => seq !== null);
  const loadedMinSeq = loadedSeqs.length ? Math.min(...loadedSeqs) : null;
  if (
    loadedMinSeq !== null &&
    normalized.messages.every((message) => {
      const seq = seqOf(message);
      return seq !== null && seq >= loadedMinSeq;
    })
  ) {
    return window;
  }
  return { pages: [...window.pages, normalized] };
}

/** 最旧的一页之前还有没有更多 */
export function hasMoreBefore(window: HistoryWindow): boolean {
  return Boolean(window.pages.at(-1)?.hasMoreBefore);
}

export function loadedMessages(window: HistoryWindow): ChatMessage[] {
  return [...window.pages].reverse().flatMap((page) => page.messages);
}

/**
 * 窗口下界。翻到会话开头时返回 `null`——此时不存在"窗口下界之下"，
 * 任何更旧的 seq 都不该被置顶（§4.3）。
 */
export function loadedFloorSeq(window: HistoryWindow): number | null {
  if (!hasMoreBefore(window)) return null;
  const seqs = loadedMessages(window)
    .map(seqOf)
    .filter((seq): seq is number => seq !== null);
  return seqs.length ? Math.min(...seqs) : null;
}

/** 这个 thread 有没有正在跑的 run（§5.1：刷新时要 join 它，而不是重发消息） */
export function findActiveRun(
  runs: readonly { run_id: string; status: string }[],
): { run_id: string; status: string } | null {
  return runs.find((run) => run.status === "pending" || run.status === "running") ?? null;
}
