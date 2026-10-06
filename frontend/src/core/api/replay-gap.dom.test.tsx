import { render, cleanup } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { createGapRecoveringFetch, registerRecoveryContext } from "./fetch-with-gap";

/**
 * gap 恢复在**真实浏览器 API** 上的行为（M5 §5.2）。
 *
 * 纯逻辑部分（帧解析、恢复顺序、次数上限）在 `gap.test.ts` 里用 node 环境覆盖了。
 * 这里只测那些**离开真实 Response / ReadableStream / React 就不存在**的东西：
 * 恢复被无缝接进同一个 body、耗尽时补的是 error 帧而不是 end 帧、
 * 以及注册表的挂载/卸载生命周期。
 */

afterEach(cleanup);

const encoder = new TextEncoder();

function sseResponse(frames: string[], headers: Record<string, string> = {}): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame));
      controller.close();
    },
  });
  return new Response(body, {
    status: 200,
    headers: { "content-type": "text/event-stream", ...headers },
  });
}

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
}

const GAP_FRAME =
  'event: gap\ndata: {"code":"stream_replay_gap","run_id":"r1","requested_event_id":"1",' +
  '"earliest_available_event_id":"5","latest_available_event_id":"9","recovery":"reload_durable_state"}\n\n';

const messageFrame = (n: number) => `event: messages\ndata: {"n":${n}}\nid: ${n}\n\n`;
const END_FRAME = "event: end\ndata: null\n\n";

async function readFrames(response: Response): Promise<{ event: string; data: string }[]> {
  const text = await response.text();
  return text
    .split("\n\n")
    .filter(Boolean)
    .flatMap((block) => {
      const event = /^event: (.*)$/m.exec(block)?.[1];
      const data = /^data: (.*)$/m.exec(block)?.[1];
      return event && data !== undefined ? [{ event, data }] : [];
    });
}

describe("gap 恢复接在 fetch 层", () => {
  it("gap 之后续订的帧接进同一个 body，SDK 侧看起来就是同一条流", async () => {
    const baseFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/join")) return sseResponse([messageFrame(10), END_FRAME]);
      // M3 §8.1：创建端必须带 Content-Location，续订点从它推出来
      return sseResponse([messageFrame(1), GAP_FRAME], {
        "content-location": "/api/threads/t1/runs/r1",
      });
    });
    const reload = vi.fn(async () => {});

    registerRecoveryContext("t1", { reloadDurableState: reload });
    const wrapped = createGapRecoveringFetch({ baseFetch: baseFetch as unknown as typeof fetch });

    const response = await wrapped("/api/threads/t1/runs/stream", { method: "POST" });
    const frames = await readFrames(response);

    expect(reload).toHaveBeenCalledTimes(1);
    expect(frames.map((f) => f.event)).toEqual(["messages", "messages", "end"]);
    expect(frames.map((f) => f.data)).toEqual(['{"n":1}', '{"n":10}', "null"]);
  });

  it("续订时带的是服务端给的保留尾部游标", async () => {
    const seen: { url?: string; lastEventId?: string | null } = {};
    const baseFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/join")) {
        seen.lastEventId = new Headers(init?.headers).get("Last-Event-ID");
        seen.url = url;
        return sseResponse([END_FRAME]);
      }
      return sseResponse([GAP_FRAME], { "content-location": "/api/threads/t2/runs/r2" });
    });

    registerRecoveryContext("t2", { reloadDurableState: async () => {} });
    const wrapped = createGapRecoveringFetch({ baseFetch: baseFetch as unknown as typeof fetch });

    await readFrames(await wrapped("/api/threads/t2/runs/stream", { method: "POST" }));

    expect(seen.url).toBe("/api/threads/t2/runs/r2/join");
    // 续订点用 latest_available_event_id，而不是从头重放整个 run
    expect(seen.lastEventId).toBe("9");
  });

  it("**恢复耗尽时补的是 error 帧，不是 end 帧**", async () => {
    const baseFetch = vi.fn(async (input: RequestInfo | URL) =>
      String(input).includes("/join")
        ? sseResponse([GAP_FRAME], { "content-location": "/api/threads/t3/runs/r3" })
        : sseResponse([messageFrame(1), GAP_FRAME], {
            "content-location": "/api/threads/t3/runs/r3",
          }),
    );

    registerRecoveryContext("t3", { reloadDurableState: async () => {}, maxRecoveries: 2 });
    const wrapped = createGapRecoveringFetch({ baseFetch: baseFetch as unknown as typeof fetch });

    const frames = await readFrames(await wrapped("/api/threads/t3/runs/stream", { method: "POST" }));

    expect(frames.some((f) => f.event === "end")).toBe(false);
    expect(frames.at(-1)!.event).toBe("error");
    expect(frames.at(-1)!.data).toContain("stream_recovery_exhausted");
  });

  it("没有注册恢复语义的流原样透传，不自作主张", async () => {
    const baseFetch = vi.fn(async () => sseResponse([messageFrame(1), END_FRAME]));
    const wrapped = createGapRecoveringFetch({ baseFetch: baseFetch as unknown as typeof fetch });

    const response = await wrapped("/api/threads/unknown/runs/stream", { method: "POST" });

    expect(response.headers.get("content-type")).toContain("text/event-stream");
    expect(baseFetch).toHaveBeenCalledTimes(1);
  });

  it("非 SSE 响应完全不碰", async () => {
    const baseFetch = vi.fn(async () => jsonResponse({ ok: true }));
    const wrapped = createGapRecoveringFetch({ baseFetch: baseFetch as unknown as typeof fetch });

    const response = await wrapped("/api/threads/t1/state");

    expect(await response.json()).toEqual({ ok: true });
  });
});

describe("恢复上下文的生命周期挂在 React 的 effect 上", () => {
  function Probe({ threadId, reload }: { threadId: string; reload: () => Promise<void> }) {
    useEffect(() => registerRecoveryContext(threadId, { reloadDurableState: reload }), [threadId, reload]);
    return null;
  }

  it("挂载时注册、卸载时注销——卸载后不能再触发恢复", async () => {
    const reload = vi.fn(async () => {});
    /**
     * 响应要带 Content-Location：续订地址是从它推出来的（M3 §8.1）。
     * 不带的话恢复会直接抛错——**这是对的**，只是这个用例要测的是注册生命周期，
     * 所以把契约补全。
     */
    const baseFetch = vi.fn(async (input: RequestInfo | URL) =>
      String(input).includes("/join")
        ? sseResponse([END_FRAME])
        : sseResponse([GAP_FRAME, END_FRAME], {
            "content-location": "/api/threads/t4/runs/r4",
          }),
    );
    const wrapped = createGapRecoveringFetch({ baseFetch: baseFetch as unknown as typeof fetch });

    const view = render(<Probe threadId="t4" reload={reload} />);
    await readFrames(await wrapped("/api/threads/t4/runs/stream", { method: "POST" }));
    expect(reload).toHaveBeenCalledTimes(1);

    view.unmount();
    await readFrames(await wrapped("/api/threads/t4/runs/stream", { method: "POST" }));
    // 卸载后恢复上下文没了，流原样透传：gap 帧被留下但没有触发重载
    expect(reload).toHaveBeenCalledTimes(1);
  });
});