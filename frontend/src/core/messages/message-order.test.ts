import { describe, expect, it } from "vitest";

import type { ChatMessage, MessageType } from "../types";
import { messageKey } from "./identity";
import {
  computeLoadedFloorSeq,
  mergeMessages,
  mergeMessagesWithDiagnostics,
  replaceMessageContent,
} from "./order";

const msg = (
  id: string,
  seq: number | null,
  over: Partial<ChatMessage> = {},
): ChatMessage => ({
  id,
  type: (over.type ?? "ai") as MessageType,
  content: id,
  seq,
  ...over,
});

const ids = (messages: readonly ChatMessage[]) => messages.map((m) => m.id);

describe("位置权威：seq 优先", () => {
  it("后到但 seq 更小的消息按 seq 落位，而不是按到达顺序落在最后", () => {
    const history = [msg("m1", 10), msg("m2", 30)];
    const stream = [msg("m15", 20)];

    expect(ids(mergeMessages({ history, stream }))).toEqual(["m1", "m15", "m2"]);
  });

  it("带 seq 的消息绝不被追加到尾部——尾部可证明是错的", () => {
    const history = [msg("m1", 10), msg("m3", 30)];
    const stream = [msg("m2", 20)];

    const result = mergeMessages({ history, stream });

    expect(result.at(-1)!.id).not.toBe("m2");
    expect(ids(result)).toEqual(["m1", "m2", "m3"]);
  });

  it("同一个身份出现多次时，最早的那个 seq 获胜", () => {
    const history = [msg("a", 50)];
    const stream = [msg("a", 20), msg("a", 35)];

    const [only] = mergeMessages({ history, stream });

    expect(only!.seq).toBe(20);
    expect(mergeMessages({ history, stream })).toHaveLength(1);
  });

  it("隐藏的控制副本只在没有可见副本携带位置时才贡献位置", () => {
    const history = [msg("x", 100)];
    const stream = [msg("x", 5, { hidden: true })];

    expect(mergeMessages({ history, stream })[0]!.seq).toBe(100);
  });

  it("只有隐藏副本时，它贡献位置但保持隐藏，不会变成可见消息", () => {
    const stream = [msg("control", 7, { hidden: true }), msg("visible", 9)];

    const result = mergeMessages({ stream });

    expect(ids(result)).toEqual(["control", "visible"]);
    expect(result[0]!.hidden).toBe(true);
    expect(result[0]!.seq).toBe(7);
  });
});

describe("低于窗口下界的实时消息：置顶，不是追加，也不是丢弃", () => {
  it("seq 低于已加载窗口下界的消息被放到所有内容的最前面", () => {
    // 300 条消息的会话，只加载了最近一页
    const history = [msg("h-251", 251), msg("h-252", 252)];
    const stream = [msg("q-42", 42, { type: "human" })];

    const result = mergeMessages({ history, stream, loadedFloorSeq: 251 });

    expect(ids(result)).toEqual(["q-42", "h-251", "h-252"]);
  });

  it("窗口已经触到会话开头时（floor=null），不存在'下界之下'，不做置顶", () => {
    const history = [msg("h-1", 1), msg("h-2", 2)];
    const stream = [msg("h-3", 3)];

    expect(ids(mergeMessages({ history, stream, loadedFloorSeq: null }))).toEqual([
      "h-1",
      "h-2",
      "h-3",
    ]);
  });

  /**
   * §4.3 那两次互相打架的修复的和解点。
   *
   * 第一次修复：带 seq 的消息被追加到尾部 → 禁止追加。
   * 第二次问题：长会话里用户的提问凭空消失 → 因为"禁止追加"的规则把它整个丢弃了。
   *
   * 和解：**被折叠的未加载间隙可以靠翻页恢复；被丢弃的消息不能。**
   * 而且这个判断必须发生在锚点遍历**之前**——压缩后的 checkpoint 可能和加载页
   * 一条身份都不共享，锚点遍历根本不会执行，而这恰恰是被压缩救回的消息
   * 最需要它的 seq 的时候。
   */
  it("压缩后零共享身份：提问仍在对话里，位置正确，一条都不少", () => {
    // 已加载的历史页：和历史页里的 checkpoint 消息零 id 重叠
    const history = [msg("page-a", 251), msg("page-b", 252), msg("page-c", 253)];
    // 压缩后的 checkpoint 只保留了当前 run 的尾巴，全新的身份
    const checkpoint = [
      msg("fresh-user", 42, { type: "human" }),
      msg("fresh-ai", 43),
      msg("fresh-tool", 44, { type: "tool", tool_call_id: "call-1" }),
    ];

    const { messages, diagnostics } = mergeMessagesWithDiagnostics({
      history,
      stream: checkpoint,
      loadedFloorSeq: 251,
    });

    // 一条都不少
    expect(messages).toHaveLength(6);
    // 位置正确：被压缩救回来的尾巴按 seq 升序排在加载页之前
    expect(ids(messages)).toEqual([
      "fresh-user",
      "fresh-ai",
      "fresh-tool",
      "page-a",
      "page-b",
      "page-c",
    ]);
    expect(diagnostics.prependedBelowFloor).toEqual([42, 43, 44]);
  });

  it("同一场景下，两种错误修复各自的症状都不会出现", () => {
    const history = [msg("page-a", 251), msg("page-b", 252)];
    const checkpoint = [msg("fresh-user", 42, { type: "human" }), msg("fresh-ai", 43)];

    const result = mergeMessages({ history, stream: checkpoint, loadedFloorSeq: 251 });
    const resultIds = ids(result);

    // 修复一的症状：追加到尾部
    expect(resultIds.slice(-2)).not.toEqual(["fresh-user", "fresh-ai"]);
    // 修复二的症状：凭空消失
    expect(resultIds).toContain("fresh-user");
    expect(resultIds).toContain("fresh-ai");
    expect(resultIds.indexOf("fresh-user")).toBeLessThan(resultIds.indexOf("page-a"));
  });
});

describe("身份锚点编织：没有 seq 的片段", () => {
  it("流式副本和历史里的同一条身份折叠，片段插在它到达前的最后一个已知位置之后", () => {
    const history = [msg("u1", 1, { type: "human" }), msg("a1", 2, { content: "he" })];
    const stream = [
      msg("a1", null, { content: "hello world" }),
      msg("a2", null, { content: " 继续" }),
    ];

    const result = mergeMessages({ history, stream });

    expect(ids(result)).toEqual(["u1", "a1", "a2"]);
    // 内容取更完整的那份，位置取历史那份的 seq
    expect(result[1]!.content).toBe("hello world");
    expect(result[1]!.seq).toBe(2);
  });

  it("片段内部顺序保留", () => {
    const history = [msg("anchor", 10)];
    const stream = [msg("s1", null), msg("s2", null), msg("s3", null)];

    expect(ids(mergeMessages({ history, stream }))).toEqual([
      "anchor",
      "s1",
      "s2",
      "s3",
    ]);
  });

  it("到达时还没有任何已知位置的消息被排在最前", () => {
    // 无 seq 的先到、带 seq 的后到 —— 前者找不到可用的锚点
    const stream = [msg("first", null), msg("later", 10)];

    expect(ids(mergeMessages({ stream }))).toEqual(["first", "later"]);
  });

  it("诊断能报出编织了几条", () => {
    const history = [msg("anchor", 10)];
    const stream = [msg("s1", null), msg("s2", null)];

    expect(
      mergeMessagesWithDiagnostics({ history, stream }).diagnostics.wovenWithoutSeq,
    ).toBe(2);
  });
});

describe("乐观消息永远在最后（§4.4）", () => {
  it("用户刚发出、服务端还没确认的那条排在所有内容之后", () => {
    const history = [msg("h1", 1), msg("h2", 2)];
    const optimistic = [msg("local", null, { type: "human", optimistic: true })];

    expect(ids(mergeMessages({ history, optimistic }))).toEqual(["h1", "h2", "local"]);
  });

  it("即使它带了 seq，也仍然在最后", () => {
    const history = [msg("h1", 100)];
    const optimistic = [msg("local", 50, { type: "human", optimistic: true })];

    expect(ids(mergeMessages({ history, optimistic }))).toEqual(["h1", "local"]);
  });

  it("确认还没到、本轮的步骤先到时，提问排在步骤**前面**", () => {
    // 线上表现：思考过程和工具调用显示在了用户提问的上方。
    const history: ChatMessage[] = [];
    const stream = [msg("step-1", null, { run_id: "run-1" })];
    const optimistic = [msg("local", null, { type: "human", optimistic: true, run_id: "run-1" })];
    const turnBaseline = {
      humanKey: "local",
      keys: new Set<string>(),
      localRunId: "run-1",
    };

    const result = mergeMessages({ history, stream, optimistic, turnBaseline });

    expect(ids(result)).toEqual(["local", "step-1"]);
  });

  it("别的 run 的步骤不会被插到提问后面（重连回来的不算本轮）", () => {
    const stream = [msg("other-run-step", null, { run_id: "run-old" })];
    const optimistic = [msg("local", null, { type: "human", optimistic: true })];
    const turnBaseline = {
      humanKey: "local",
      keys: new Set<string>(),
      localRunId: "run-1",
    };

    const result = mergeMessages({ stream, optimistic, turnBaseline });

    expect(ids(result)).toEqual(["other-run-step", "local"]);
  });

  it("服务端确认之后不会出现两条：乐观副本按身份被去掉", () => {
    // 提交时客户端发了 id=X，M6 的动态上下文中间件把它改名成 X__user
    const optimistic = [msg("X", null, { type: "human", optimistic: true, content: "你好" })];
    const history = [msg("X__user", 7, { type: "human", content: "你好" })];

    const result = mergeMessages({ history, optimistic });

    expect(result).toHaveLength(1);
    expect(result[0]!.seq).toBe(7);
  });
});

describe("本地提交那一轮的身份基线（§4.4）", () => {
  it("流先发布了新的 AI 步骤时，非基线步骤被移到新的人类消息之后", () => {
    // 历史里已经有的两条，属于基线
    const history = [msg("old-human", 1, { type: "human" }), msg("old-ai", 2)];
    // 流里：新的 AI 步骤先到，人类消息后到
    const stream = [
      msg("new-ai", null, { run_id: "run-1" }),
      msg("new-human", null, { type: "human", run_id: "run-1" }),
    ];

    const result = mergeMessages({
      history,
      stream,
      turnBaseline: {
        humanKey: "new-human",
        keys: new Set(["old-human", "old-ai"]),
        localRunId: "run-1",
      },
    });

    expect(ids(result)).toEqual(["old-human", "old-ai", "new-human", "new-ai"]);
  });

  it("基线内的历史步骤不动", () => {
    const history = [msg("h1", 1, { type: "human" }), msg("a1", 2)];
    const stream = [msg("a1", null), msg("h2", null, { type: "human", run_id: "r" })];

    const result = mergeMessages({
      history,
      stream,
      turnBaseline: { humanKey: "h2", keys: new Set(["h1", "a1"]), localRunId: "r" },
    });

    expect(ids(result)).toEqual(["h1", "a1", "h2"]);
  });

  it("重连 join 回来的 run 的步骤不动", () => {
    const stream = [
      msg("reconnected", null, { run_id: "other-run" }),
      msg("new-human", null, { type: "human", run_id: "run-1" }),
    ];

    const result = mergeMessages({
      stream,
      turnBaseline: { humanKey: "new-human", keys: new Set(), localRunId: "run-1" },
    });

    expect(ids(result)).toEqual(["reconnected", "new-human"]);
  });

  it("隐藏的控制消息不动", () => {
    const stream = [
      msg("control", null, { hidden: true, run_id: "run-1" }),
      msg("new-human", null, { type: "human", run_id: "run-1" }),
    ];

    const result = mergeMessages({
      stream,
      turnBaseline: { humanKey: "new-human", keys: new Set(), localRunId: "run-1" },
    });

    expect(ids(result)).toEqual(["control", "new-human"]);
  });
});

describe("动态上下文给用户消息改 key（§4.4）", () => {
  it("提交帧 X 和 checkpoint 替换 X__user 是同一个可见轮次", () => {
    const stream = [msg("X", null, { type: "human", content: "提交" })];
    const history = [msg("X__user", 5, { type: "human", content: "提交" })];

    const result = mergeMessages({ history, stream });

    expect(result).toHaveLength(1);
    expect(result[0]!.seq).toBe(5);
  });

  it("这个归一化只对**人类**消息生效", () => {
    const toolCall = { id: "call__user", name: "read_file", args: {} };
    const human = msg("X", null, { type: "human" });
    const ai = msg("X__user", 3, { tool_calls: [toolCall] });

    // 不同 type ⇒ 不同身份，不会被合并
    expect(messageKey(human)).toBe("X");
    expect(messageKey(ai)).toBe("X__user");
    expect(mergeMessages({ history: [ai], stream: [human] })).toHaveLength(2);
  });
});

describe("内容替换不丢元数据（§4.4）", () => {
  it("只更新正文时，已知的 seq、run_id 和运行时长要保留", () => {
    const previous = msg("a1", 12, { run_id: "run-9", duration_ms: 4200 });
    const next: ChatMessage = { id: "a1", type: "ai", content: "压缩后重建的正文" };

    const merged = replaceMessageContent(previous, next);

    expect(merged.content).toBe("压缩后重建的正文");
    expect(merged.seq).toBe(12);
    expect(merged.run_id).toBe("run-9");
    expect(merged.duration_ms).toBe(4200);
  });

  it("新值存在时不覆盖", () => {
    const previous = msg("a1", 12, { run_id: "old" });
    const next = msg("a1", 99, { run_id: "new" });

    const merged = replaceMessageContent(previous, next);

    expect(merged.seq).toBe(99);
    expect(merged.run_id).toBe("new");
  });

  it("没有上一条时原样返回", () => {
    const next = msg("a1", 1);
    expect(replaceMessageContent(undefined, next)).toBe(next);
  });
});

describe("computeLoadedFloorSeq", () => {
  it("还有更早的页时，下界是已加载 seq 的最小值", () => {
    const pages = [[msg("a", 251)], [msg("b", 252), msg("c", 253)]];

    expect(computeLoadedFloorSeq(pages, true)).toBe(251);
  });

  it("已经翻到会话开头时没有下界", () => {
    expect(computeLoadedFloorSeq([[msg("a", 1)]], false)).toBeNull();
  });

  it("什么都没加载时没有下界", () => {
    expect(computeLoadedFloorSeq([], true)).toBeNull();
  });
});
