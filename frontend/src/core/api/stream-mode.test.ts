import { describe, expect, it } from "vitest";

import {
  INCREMENTAL_STREAM_MODES,
  UnsupportedStreamModeError,
  assertSupportedStreamModes,
  isIncrementalOnly,
  isStreamingRunRequest,
  rewriteStreamModes,
  sanitizeChatStreamModes,
} from "./stream-mode";

describe("不支持的模式抛错，而不是静默退回 values", () => {
  it("拼错的模式名直接抛错", () => {
    expect(() => sanitizeChatStreamModes(["messages-tuple", "messages_tuple"])).toThrow(
      UnsupportedStreamModeError,
    );
  });

  it("抛出的错列出真正支持的模式，便于定位", () => {
    let caught: UnsupportedStreamModeError | null = null;
    try {
      assertSupportedStreamModes(["nope"]);
    } catch (error) {
      caught = error as UnsupportedStreamModeError;
    }

    expect(caught).toBeInstanceOf(UnsupportedStreamModeError);
    expect(caught!.unsupported).toEqual(["nope"]);
    expect(caught!.message).toContain("values");
  });

  it("不做部分转发：一个不支持的模式就让整次请求失败", () => {
    expect(() => sanitizeChatStreamModes(["updates", "bogus"])).toThrow(
      UnsupportedStreamModeError,
    );
  });

  it("extra 里的不支持模式同样抛错", () => {
    expect(() => sanitizeChatStreamModes([], { extra: ["tasks_v2"] })).toThrow(
      UnsupportedStreamModeError,
    );
  });
});

describe("主聊天流被强制成增量模式", () => {
  it("默认就是 messages-tuple + updates + custom，不带 values", () => {
    const modes = sanitizeChatStreamModes(null);

    expect(new Set(modes)).toEqual(new Set(INCREMENTAL_STREAM_MODES));
    expect(modes).not.toContain("values");
  });

  it("SDK 因为惰性追踪自己加上的 values 被强制覆盖掉", () => {
    const modes = sanitizeChatStreamModes(["values", "messages-tuple", "updates", "custom"]);

    expect(modes).not.toContain("values");
    expect(isIncrementalOnly(modes)).toBe(true);
  });

  it("**messages-tuple 不能省**——文件工具的分块合批挂在它上面", () => {
    // 一次几十轮的 run 里，去掉快照模式是对的；
    // 但顺手把 messages-tuple 也去掉，写一个大文件会退化成每个 token 一帧 SSE。
    for (const requested of [null, [], ["updates"], ["values", "custom"]]) {
      expect(sanitizeChatStreamModes(requested)).toContain("messages-tuple");
    }
  });

  it("显式的状态检查（gap 恢复）仍然可以用 values", () => {
    const modes = sanitizeChatStreamModes(null, { includeValues: true });

    expect(modes).toContain("values");
    expect(modes).toContain("messages-tuple");
  });

  it("用户显式请求的非快照模式保留", () => {
    const modes = sanitizeChatStreamModes(null, { extra: ["debug", "tasks", "checkpoints"] });

    expect(modes).toEqual(expect.arrayContaining(["debug", "tasks", "checkpoints"]));
    expect(modes).not.toContain("values");
  });

  it("extra 里混进 values 时，只有 includeValues 才能真正打开它", () => {
    expect(sanitizeChatStreamModes(null, { extra: ["values"] })).not.toContain("values");
  });
});
describe("请求发出前的强制覆盖（§3.1）", () => {
  const body = (streamMode: unknown) =>
    JSON.stringify({ assistant_id: "lead_agent", input: { messages: [] }, stream_mode: streamMode });

  it("SDK 自己加上的 values 在最后一刻被改掉", () => {
    const rewritten = JSON.parse(rewriteStreamModes(body(["values", "messages-tuple", "updates"])));

    expect(rewritten.stream_mode).not.toContain("values");
    expect(rewritten.stream_mode).toContain("messages-tuple");
  });

  it("调用方完全没声明 stream_mode 时补上增量模式——服务端默认是 values", () => {
    const rewritten = JSON.parse(
      rewriteStreamModes(JSON.stringify({ assistant_id: "lead_agent" })),
    );

    expect(new Set(rewritten.stream_mode)).toEqual(new Set(INCREMENTAL_STREAM_MODES));
  });

  it("已经是对的 body 原样返回，不做无谓的重建", () => {
    const original = body([...INCREMENTAL_STREAM_MODES]);

    expect(rewriteStreamModes(original)).toBe(original);
  });

  it("body 里带不支持的模式时抛错，而不是悄悄改掉", () => {
    expect(() => rewriteStreamModes(body(["messages_tuple"]))).toThrow(UnsupportedStreamModeError);
  });

  it("非 JSON body 原样放过（比如上传）", () => {
    expect(rewriteStreamModes("not json")).toBe("not json");
  });

  it("只对流式 run 的创建请求生效", () => {
    expect(isStreamingRunRequest("/api/threads/t1/runs/stream")).toBe(true);
    expect(isStreamingRunRequest("/api/threads/t1/runs/r1/stream")).toBe(true);
    // 显式的状态检查是 §3.1 的例外，不能碰
    expect(isStreamingRunRequest("/api/threads/t1/state")).toBe(false);
    expect(isStreamingRunRequest("/api/threads/t1/runs/wait")).toBe(false);
    expect(isStreamingRunRequest("/api/threads/t1/runs")).toBe(false);
  });
});
