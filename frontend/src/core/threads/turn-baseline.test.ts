import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../types";
import { createTurnBaseline, hasNewTurnMessages, reconcileOptimistic } from "./turn-baseline";

const msg = (id: string, over: Partial<ChatMessage> = {}): ChatMessage => ({
  id,
  type: (over.type ?? "ai") as ChatMessage["type"],
  content: id,
  seq: null,
  ...over,
});

describe("乐观消息的三种处置", () => {
  const optimistic = (text = "你好"): ChatMessage[] => [
    { id: "local-1", type: "human", content: text, seq: null, optimistic: true },
  ];

  it("服务端还没反应：原样保留，仍显示发送中", () => {
    const baseline = createTurnBaseline([], "local-1", "run-1");
    const state = reconcileOptimistic(optimistic(), [], baseline);

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]!.optimistic).toBe(true);
    expect(state.pending).toBe(true);
  });

  it("**换了 id 但正文相同**也算回显：去掉乐观副本，不留两条一模一样的提问", () => {
    const baseline = createTurnBaseline([], "local-1", "run-1");
    const stream = [msg("human-x", { type: "human", content: "你好" })];

    const state = reconcileOptimistic(optimistic(), stream, baseline);

    expect(state.messages).toEqual([]);
    expect(state.pending).toBe(false);
  });

  it("步骤先到、提问还没回来：保留提问，但摘掉发送中", () => {
    const baseline = createTurnBaseline([], "local-1", "run-1");
    const stream = [msg("step-1", { run_id: "run-1" })];

    const state = reconcileOptimistic(optimistic(), stream, baseline);

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]!.optimistic).toBe(false);
    expect(state.pending).toBe(false);
  });

  it("基线里的旧消息不算回显", () => {
    const previous = msg("old-human", { type: "human", content: "你好" });
    const baseline = createTurnBaseline([previous], "local-1", "run-1");

    const state = reconcileOptimistic(optimistic(), [previous], baseline);

    expect(state.messages[0]!.optimistic).toBe(true);
    expect(state.pending).toBe(true);
  });
});

describe("本轮是否已经看得见地开始了", () => {
  it("没有流消息时不算开始", () => {
    const baseline = createTurnBaseline([msg("old")], "local", "run-1");
    expect(hasNewTurnMessages([], baseline)).toBe(false);
  });

  it("只有基线里的消息也不算开始", () => {
    const previous = msg("old");
    const baseline = createTurnBaseline([previous], "local", "run-1");
    expect(hasNewTurnMessages([previous], baseline)).toBe(false);
  });

  it("**只来了 AI 步骤也算开始**——否则乐观消息会一直挂“发送中…”", () => {
    const baseline = createTurnBaseline([msg("old")], "local", "run-1");
    const step = msg("step-1", { run_id: "run-1" });

    expect(hasNewTurnMessages([step], baseline)).toBe(true);
  });

  it("隐藏的控制消息不算", () => {
    const baseline = createTurnBaseline([], "local", "run-1");
    expect(hasNewTurnMessages([msg("hidden", { hidden: true })], baseline)).toBe(false);
  });

  it("没有基线（刷新后续流）时按到达即确认处理", () => {
    expect(hasNewTurnMessages([msg("anything")], null)).toBe(true);
  });
});
