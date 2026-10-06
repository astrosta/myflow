import { describe, expect, it, vi } from "vitest";

import {
  StreamRecoveryExhaustedError,
  asGapFrame,
  formatSseFrame,
  nextSubscriptionCursor,
  parseSseStream,
  withGapRecovery,
  type SseFrame,
} from "./gap";

async function collect<T>(source: AsyncIterable<T>): Promise<T[]> {
  const out: T[] = [];
  for await (const item of source) out.push(item);
  return out;
}

async function* fromArray<T>(items: readonly T[]): AsyncIterable<T> {
  for (const item of items) yield item;
}

async function* fromText(text: string): AsyncIterable<string> {
  // 逐字符喂，确认解析器不依赖 chunk 边界
  for (const char of text) yield char;
}

const SSE_SAMPLE = [
  'event: metadata\ndata: {"run_id":"r1","thread_id":"t1"}\nid: 1\n\n',
  'event: messages\ndata: {"chunk":1}\nid: 2\n\n',
  ": heartbeat\n\n",
  'event: end\ndata: null\n\n',
].join("");

describe("SSE 帧解析（M3 §8.1 的 wire 格式）", () => {
  it("解析出 event / data / id", async () => {
    const frames = await collect(parseSseStream(fromText(SSE_SAMPLE)));

    expect(frames.map((f) => f.event)).toEqual(["metadata", "messages", "end"]);
    expect(frames[0]).toMatchObject({ id: "1" });
    expect(frames[1]!.data).toBe('{"chunk":1}');
  });

  it("注释行（心跳）被忽略，不产生帧", async () => {
    const frames = await collect(parseSseStream(fromText(": heartbeat\n\n")));

    expect(frames).toHaveLength(0);
  });

  it("心跳没有 id，不能当续传游标", async () => {
    const frames = await collect(parseSseStream(fromText(": heartbeat\n\nevent: end\ndata: null\n\n")));

    expect(frames).toHaveLength(1);
    expect(frames[0]!.id).toBeNull();
  });

  it("字段顺序 event -> data -> id 能被原样编码回去", () => {
    expect(formatSseFrame({ event: "messages", data: "{}", id: "7" })).toBe(
      "event: messages\ndata: {}\nid: 7\n\n",
    );
  });

  it("没有 id 时不写 id 行", () => {
    expect(formatSseFrame({ event: "end", data: "null", id: null })).toBe("event: end\ndata: null\n\n");
  });
});

describe("gap 帧", () => {
  const gapFrame: SseFrame = {
    event: "gap",
    data: JSON.stringify({
      code: "stream_replay_gap",
      run_id: "r1",
      requested_event_id: "40",
      earliest_available_event_id: "90",
      latest_available_event_id: "120",
      recovery: "reload_durable_state",
    }),
    id: null,
  };

  it("三个 id 都透传，供客户端诊断", () => {
    expect(asGapFrame(gapFrame)).toEqual({
      code: "stream_replay_gap",
      run_id: "r1",
      requested_event_id: "40",
      earliest_available_event_id: "90",
      latest_available_event_id: "120",
      recovery: "reload_durable_state",
    });
  });

  it("非 gap 帧返回 null", () => {
    expect(asGapFrame({ event: "messages", data: "{}", id: "1" })).toBeNull();
  });

  it("data 不是 JSON 时不炸，退化成三个 null", () => {
    expect(asGapFrame({ event: "gap", data: "<html>502</html>", id: null })).toMatchObject({
      requested_event_id: null,
      earliest_available_event_id: null,
      latest_available_event_id: null,
    });
  });

  it("续订点取保留窗口的上界", () => {
    expect(nextSubscriptionCursor(asGapFrame(gapFrame)!)).toBe("120");
  });

  it("缓冲区空了就不带游标（等价于从头订）", () => {
    const empty = asGapFrame({
      event: "gap",
      data: JSON.stringify({
        code: "stream_replay_gap",
        requested_event_id: null,
        earliest_available_event_id: null,
        latest_available_event_id: null,
        recovery: "reload_durable_state",
      }),
      id: null,
    })!;

    expect(nextSubscriptionCursor(empty)).toBeNull();
  });
});

describe("收到 gap 之后的恢复顺序（§5.2）", () => {
  const gap: SseFrame = {
    event: "gap",
    data: JSON.stringify({
      code: "stream_replay_gap",
      requested_event_id: "1",
      earliest_available_event_id: "5",
      latest_available_event_id: "9",
      recovery: "reload_durable_state",
    }),
    id: null,
  };
  const frame = (n: number): SseFrame => ({ event: "messages", data: `{"n":${n}}`, id: String(n) });
  const end: SseFrame = { event: "end", data: "null", id: null };

  it("清游标 → 通知上层 → 重载状态 → 续订，顺序不能乱", async () => {
    const calls: string[] = [];
    const frames = await collect(
      withGapRecovery(fromArray([frame(1), gap]), {
        classify: asGapFrame,
        onResetCursor: () => calls.push("reset-cursor"),
        onRecovery: () => calls.push("notify"),
        reloadDurableState: async () => {
          calls.push("reload");
        },
        resubscribe: (cursor) => {
          calls.push(`resubscribe:${cursor}`);
          return fromArray([frame(10), end]);
        },
      }),
    );

    expect(calls).toEqual(["reset-cursor", "notify", "reload", "resubscribe:9"]);
    expect(frames.map((f) => f.id)).toEqual(["1", "10", null]);
  });

  it("gap 之后续上的帧照常吐出，gap 本身不会被透传给 SDK", async () => {
    const frames = await collect(
      withGapRecovery(fromArray([gap]), {
        classify: asGapFrame,
        reloadDurableState: async () => {},
        resubscribe: () => fromArray([frame(10), end]),
      }),
    );

    expect(frames.some((f) => f.event === "gap")).toBe(false);
    expect(frames.map((f) => f.event)).toEqual(["messages", "end"]);
  });

  it("**绝不能让 gap 表现为一次正常的流结束**", async () => {
    const frames = await collect(
      withGapRecovery(fromArray([frame(1), gap]), {
        classify: asGapFrame,
        reloadDurableState: async () => {},
        resubscribe: () => fromArray([frame(10)]), // 续订的流没有 end
      }),
    );

    // 恢复后流自然关闭，而不是被塞一个 end
    expect(frames.some((f) => f.event === "end")).toBe(false);
    expect(frames.map((f) => f.id)).toEqual(["1", "10"]);
  });

  it("正常结束的流不触发恢复", async () => {
    const reload = vi.fn(async () => {});
    const frames = await collect(
      withGapRecovery(fromArray([frame(1), end]), {
        classify: asGapFrame,
        reloadDurableState: reload,
        resubscribe: () => {
          throw new Error("不该续订");
        },
      }),
    );

    expect(reload).not.toHaveBeenCalled();
    expect(frames.map((f) => f.event)).toEqual(["messages", "end"]);
  });

  it("恢复次数有上限，避免无限循环", async () => {
    const reload = vi.fn(async () => {});
    const resubscribe = vi.fn(() => fromArray([gap]));

    await expect(
      collect(
        withGapRecovery(fromArray([gap]), {
          classify: asGapFrame,
          reloadDurableState: reload,
          resubscribe,
          maxRecoveries: 5,
        }),
      ),
    ).rejects.toBeInstanceOf(StreamRecoveryExhaustedError);

    expect(resubscribe).toHaveBeenCalledTimes(5);
    expect(reload).toHaveBeenCalledTimes(5);
  });

  it("恢复耗尽时最后通知上层 exhausted，而不是静默结束", async () => {
    const events: boolean[] = [];

    await expect(
      collect(
        withGapRecovery(fromArray([gap]), {
          classify: asGapFrame,
          onRecovery: (event) => events.push(event.exhausted),
          reloadDurableState: async () => {},
          resubscribe: () => fromArray([gap]),
          maxRecoveries: 2,
        }),
      ),
    ).rejects.toBeInstanceOf(StreamRecoveryExhaustedError);

    expect(events).toEqual([false, false, true]);
  });

  it("恢复期间的恢复事件带 attempt 计数，供 UI 显示提示", async () => {
    const seen: number[] = [];

    await collect(
      withGapRecovery(fromArray([gap]), {
        classify: asGapFrame,
        onRecovery: (event) => seen.push(event.attempt),
        reloadDurableState: async () => {},
        resubscribe: () => fromArray([end]),
      }),
    );

    expect(seen).toEqual([1]);
  });

  it("是一份惰性异步可迭代：不消费就不发请求", async () => {
    const resubscribe = vi.fn(() => fromArray<SseFrame>([end]));
    const source = withGapRecovery(fromArray([frame(1), end]), {
      classify: asGapFrame,
      reloadDurableState: async () => {},
      resubscribe,
    });

    expect(resubscribe).not.toHaveBeenCalled();
    await collect(source);
    expect(resubscribe).not.toHaveBeenCalled();
  });
});
