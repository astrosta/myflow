import { describe, expect, it } from "vitest";

import {
  StateReducerConflictError,
  createEmptyThreadState,
  foldUpdateFrame,
  isRelevantUpdateFrame,
  mergeArtifacts,
  mergeSandbox,
  mergeTodos,
  mergeViewedImages,
} from "./stream-state";

describe("累加去重：artifacts / promoted", () => {
  it("去重且保序（用 dict.fromkeys 的语义）", () => {
    expect(mergeArtifacts(["a", "b"], ["b", "c", "a", "d"])).toEqual(["a", "b", "c", "d"]);
  });

  it("foldUpdateFrame 走的是同一套语义", () => {
    const state = foldUpdateFrame(createEmptyThreadState(), { artifacts: ["x", "y"] });
    const next = foldUpdateFrame(state, { artifacts: ["y", "z"] });

    expect(next.artifacts).toEqual(["x", "y", "z"]);
  });
});

describe("幂等写，冲突即 bug：sandbox 是 fail closed 的", () => {
  it("同值放行", () => {
    const existing = { sandbox_id: "sbx-1" };
    expect(mergeSandbox(existing, { sandbox_id: "sbx-1" })?.sandbox_id).toBe("sbx-1");
  });

  it("异值抛异常，不静默选一个", () => {
    expect(() => mergeSandbox({ sandbox_id: "sbx-1" }, { sandbox_id: "sbx-2" })).toThrow(
      StateReducerConflictError,
    );
  });

  it("null 是「没动」", () => {
    expect(mergeSandbox({ sandbox_id: "sbx-1" }, null)?.sandbox_id).toBe("sbx-1");
  });

  it("同一 thread 里出现两个 sandbox_id 会让整帧折叠失败，而不是前后端状态不一致", () => {
    const state = foldUpdateFrame(createEmptyThreadState(), { sandbox: { sandbox_id: "a" } });

    expect(() => foldUpdateFrame(state, { sandbox: { sandbox_id: "b" } })).toThrow(
      StateReducerConflictError,
    );
  });
});

describe("最后非 None 获胜：todos", () => {
  it("None = 没动", () => {
    expect(mergeTodos([{ content: "a" }], null)).toEqual([{ content: "a" }]);
  });

  it("[] = 显式清空。两者不同", () => {
    expect(mergeTodos([{ content: "a" }], [])).toEqual([]);
  });

  it("新列表覆盖旧列表", () => {
    expect(mergeTodos([{ content: "a" }], [{ content: "b" }])).toEqual([{ content: "b" }]);
  });
});

describe("合并 + 空字典清空：viewed_images", () => {
  it("{} 是「清空」信号，不是「没动」", () => {
    expect(mergeViewedImages({ a: { v: 1 } }, {})).toEqual({});
  });

  it("非空字典做合并", () => {
    expect(mergeViewedImages({ a: { v: 1 } }, { b: { v: 2 } })).toEqual({
      a: { v: 1 },
      b: { v: 2 },
    });
  });
});

describe("委派台账：按 id 合并，终态不会被 in_progress 复活", () => {
  it("对象条目不会被当成非字符串丢掉", () => {
    const next = foldUpdateFrame(createEmptyThreadState(), {
      tools: {
        delegations: [
          {
            id: "call-1",
            description: "调研竞品",
            subagent_type: "researcher",
            status: "in_progress",
            created_at: "2026-11-09T00:00:00Z",
          },
        ],
      },
    });

    expect(next.delegations).toHaveLength(1);
    expect(next.delegations?.[0]).toMatchObject({ id: "call-1", status: "in_progress" });
  });

  it("同一 id 的终态覆盖进行中；迟到的进行中不会把终态复活", () => {
    let state = foldUpdateFrame(createEmptyThreadState(), {
      delegations: [{ id: "call-1", status: "in_progress", created_at: "t0" }],
    });
    state = foldUpdateFrame(state, {
      delegations: [{ id: "call-1", status: "completed", result_brief: "结论" }],
    });
    state = foldUpdateFrame(state, {
      delegations: [{ id: "call-1", status: "in_progress" }],
    });

    expect(state.delegations).toEqual([
      {
        id: "call-1",
        status: "completed",
        result_brief: "结论",
        created_at: "t0",
      },
    ]);
  });
});

describe("无关的帧在调用 SDK 的状态改写器之前就被拒掉", () => {
  it("帧里一个 ThreadState 字段都没有 → 不相关", () => {
    expect(isRelevantUpdateFrame({ some_node: { unrelated_field: 1 } })).toBe(false);
  });

  it("直接挂在根上的 ThreadState 字段 → 相关", () => {
    expect(isRelevantUpdateFrame({ artifacts: ["a"] })).toBe(true);
  });

  it("包在节点名下面 → 相关", () => {
    expect(isRelevantUpdateFrame({ tools_node: { artifacts: ["a"] } })).toBe(true);
  });

  it("不相关的帧不改变 state", () => {
    const state = createEmptyThreadState();
    const next = foldUpdateFrame(state, { agent: { something_else: true } });

    expect(next).toBe(state);
  });

  it("不相关的 key 不会被顺手塞进 state", () => {
    const next = foldUpdateFrame(createEmptyThreadState(), { artifacts: ["a"], stray: ["nope"] });

    expect(next.artifacts).toEqual(["a"]);
    expect(Object.keys(next)).not.toContain("stray");
  });
});

describe("messages 通道的唯一所有者是 SDK 的 messages-tuple 管理器", () => {
  it("foldUpdateFrame 不折 messages", () => {
    const state = createEmptyThreadState();
    const next = foldUpdateFrame(state, { messages: [{ id: "m1" }] });

    expect(next.messages).toEqual([]);
  });
});
