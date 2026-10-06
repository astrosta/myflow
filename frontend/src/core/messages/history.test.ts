import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../types";
import {
  createHistoryWindow,
  findActiveRun,
  hasMoreBefore,
  loadedFloorSeq,
  loadedMessages,
  prependOlderPage,
  replaceNewestPage,
  type MessagePage,
} from "./history";

const msg = (seq: number): ChatMessage => ({ id: `m${seq}`, type: "ai", content: `m${seq}`, seq });

const page = (seqs: number[], more: boolean, next: number | null = null): MessagePage => ({
  messages: seqs.map(msg),
  hasMoreBefore: more,
  nextBeforeSeq: next,
});

describe("翻页", () => {
  it("更旧的一页插到后面，读出来是升序的", () => {
    let window = createHistoryWindow();
    window = replaceNewestPage(window, page([251, 252, 253], true, 250));
    window = prependOlderPage(window, page([248, 249, 250], true, 247));

    expect(loadedMessages(window).map((m) => m.seq)).toEqual([
      248, 249, 250, 251, 252, 253,
    ]);
  });

  it("重复加载同一页不会插两遍", () => {
    let window = createHistoryWindow();
    window = replaceNewestPage(window, page([10, 11], true));
    const once = prependOlderPage(window, page([8, 9], true));
    const twice = prependOlderPage(once, page([8, 9], true));

    expect(loadedMessages(twice).map((m) => m.seq)).toEqual([8, 9, 10, 11]);
  });

  it("空的一页不改变窗口", () => {
    const window = replaceNewestPage(createHistoryWindow(), page([1, 2], true));
    expect(prependOlderPage(window, page([], false))).toBe(window);
  });
});

describe("历史缓存失效时保留已加载的页（§4.4）", () => {
  it("重取第一页时，已经加载的更旧页还在", () => {
    let window = createHistoryWindow();
    window = replaceNewestPage(window, page([251, 252], true));
    window = prependOlderPage(window, page([249, 250], true));

    // 这一轮又写了两条消息，第一页整体前移
    const refreshed = replaceNewestPage(window, page([253, 254], true));

    expect(loadedMessages(refreshed).map((m) => m.seq)).toEqual([249, 250, 253, 254]);
  });

  it("已经建立的排序位置不会被丢掉，列表不会重新洗牌", () => {
    let window = createHistoryWindow();
    window = replaceNewestPage(window, page([251, 252], true));
    window = prependOlderPage(window, page([249, 250], true));
    const before = loadedMessages(window).map((m) => m.seq);

    const refreshed = replaceNewestPage(window, page([252, 253], true));
    const after = loadedMessages(refreshed).map((m) => m.seq);

    // 旧页原样保留，新页接在后面
    expect(after.slice(0, before.length - 2)).toEqual(before.slice(0, before.length - 2));
  });

  it("新的第一页覆盖到更旧页范围时，重合的旧页被丢掉，不出现两遍", () => {
    let window = createHistoryWindow();
    window = replaceNewestPage(window, page([251, 252], true));
    window = prependOlderPage(window, page([249, 250], true));

    // 一次性拉了很大一页，把 249..254 全包了
    const refreshed = replaceNewestPage(window, page([249, 250, 251, 252, 253, 254], true));

    expect(loadedMessages(refreshed).map((m) => m.seq)).toEqual([
      249, 250, 251, 252, 253, 254,
    ]);
  });
});

describe("窗口下界", () => {
  it("还有更早的页时给出下界", () => {
    const window = replaceNewestPage(createHistoryWindow(), page([251, 252, 253], true, 250));

    expect(loadedFloorSeq(window)).toBe(251);
    expect(hasMoreBefore(window)).toBe(true);
  });

  it("已经翻到会话开头时没有下界——不存在「窗口下界之下」", () => {
    let window = replaceNewestPage(createHistoryWindow(), page([10, 11], true));
    window = prependOlderPage(window, page([1, 2], false));

    expect(loadedFloorSeq(window)).toBeNull();
    expect(hasMoreBefore(window)).toBe(false);
  });

  it("什么都没加载时没有下界", () => {
    expect(loadedFloorSeq(createHistoryWindow())).toBeNull();
  });
});

describe("findActiveRun（§5.1）", () => {
  it("找到正在跑的 run，刷新时应该 join 它而不是重发消息", () => {
    const runs = [
      { run_id: "r1", status: "success" },
      { run_id: "r2", status: "running" },
    ];

    expect(findActiveRun(runs)?.run_id).toBe("r2");
  });

  it("pending 也算", () => {
    expect(findActiveRun([{ run_id: "r1", status: "pending" }])?.run_id).toBe("r1");
  });

  it("都结束了就是 null", () => {
    expect(findActiveRun([{ run_id: "r1", status: "success" }])).toBeNull();
  });
});
