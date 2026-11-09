import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../types";
import {
  applySubagentEvent,
  buildSubagentView,
  subagentStatusLabel,
  type SubagentInput,
  type SubagentLiveState,
} from "./subagent";

function toolResult(over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "tool-1",
    type: "tool",
    content: "任务成功。\n结果：结论",
    tool_call_id: "call-1",
    additional_kwargs: {
      subagent_status: "completed",
      subagent_result_brief: "结论文本",
      subagent_model_name: "deepseek-chat",
      subagent_token_usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
    },
    ...over,
  };
}

function taskCall(over: Partial<SubagentInput> = {}): SubagentInput {
  return {
    id: "call-1",
    name: "task",
    args: {
      description: "调研竞品",
      subagent_type: "researcher",
      prompt: "请调研竞品\n并给出结论",
    },
    status: "success",
    result: toolResult(),
    resultText: "任务成功。\n结果：结论",
    ...over,
  };
}

describe("子任务卡片数据", () => {
  it("描述优先用 description，状态和结论来自工具结果", () => {
    const view = buildSubagentView(taskCall());

    expect(view.description).toBe("调研竞品");
    expect(view.subagentType).toBe("researcher");
    expect(view.status).toBe("completed");
    expect(view.resultText).toBe("结论文本");
    expect(view.modelName).toBe("deepseek-chat");
    expect(view.usage?.total_tokens).toBe(15);
  });

  it("没有 description 时取 prompt 第一行；还没结果时是运行中", () => {
    const view = buildSubagentView(
      taskCall({
        args: { prompt: "第一行简报\n第二行细节", subagent_type: "general-purpose" },
        status: "pending",
        result: undefined,
        resultText: "",
      }),
    );

    expect(view.description).toBe("第一行简报");
    expect(view.status).toBe("running");
    expect(view.statusLabel).toBe("运行中");
  });

  it("台账里的 in_progress 映射成运行中，不暴露内部状态名", () => {
    const view = buildSubagentView(
      taskCall({ status: "pending", result: undefined, resultText: "" }),
      {
        delegation: {
          id: "call-1",
          description: "台账描述",
          subagent_type: "researcher",
          status: "in_progress",
          created_at: "2026-11-09T00:00:00Z",
        },
      },
    );

    expect(view.status).toBe("running");
    expect(view.statusLabel).toBe("运行中");
    expect(view.startedAt).toBe("2026-11-09T00:00:00Z");
  });

  it("实时终态事件可以补出失败原因和部分结果", () => {
    const live: SubagentLiveState = {
      taskId: "call-1",
      status: "failed",
      error: "子代理执行失败",
      result: "已经拿到的部分结论",
      steps: [],
    };
    const view = buildSubagentView(
      taskCall({ status: "pending", result: undefined, resultText: "" }),
      { live },
    );

    expect(view.status).toBe("failed");
    expect(view.error).toBe("子代理执行失败");
    expect(view.resultText).toBe("已经拿到的部分结论");
  });

  it("状态标签覆盖全部终态", () => {
    expect(subagentStatusLabel("completed")).toBe("已完成");
    expect(subagentStatusLabel("failed")).toBe("失败");
    expect(subagentStatusLabel("cancelled")).toBe("已取消");
    expect(subagentStatusLabel("timed_out")).toBe("已超时");
    expect(subagentStatusLabel("polling_timed_out")).toBe("等待超时");
  });
});

describe("子任务实时事件", () => {
  it("汇总 started / running / completed，重复步骤只记一次", () => {
    let state: Record<string, SubagentLiveState> = {};

    state = applySubagentEvent(state, {
      type: "task_started",
      task_id: "call-1",
      description: "调研竞品",
      subagent_type: "researcher",
      model_name: "deepseek-chat",
    });
    expect(state["call-1"]?.status).toBe("running");
    expect(state["call-1"]?.description).toBe("调研竞品");

    const step = {
      type: "task_running",
      task_id: "call-1",
      message_index: 3,
      message: {
        type: "tool",
        id: "sub-tool-1",
        name: "web_search",
        content: "搜索结果",
      },
    };
    state = applySubagentEvent(state, step);
    state = applySubagentEvent(state, step);
    expect(state["call-1"]?.steps).toHaveLength(1);
    expect(state["call-1"]?.steps[0]).toMatchObject({
      index: 3,
      kind: "tool",
      name: "web_search",
      text: "搜索结果",
    });

    state = applySubagentEvent(state, {
      type: "task_completed",
      task_id: "call-1",
      status: "completed",
      result: "完整结论",
      usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
    });
    expect(state["call-1"]?.status).toBe("completed");
    expect(state["call-1"]?.result).toBe("完整结论");
    expect(state["call-1"]?.usage?.total_tokens).toBe(15);
  });

  it("忽略不属于子任务的 custom 事件", () => {
    const state: Record<string, SubagentLiveState> = {};
    expect(applySubagentEvent(state, { type: "tool_progress", task_id: "x" })).toBe(state);
    expect(applySubagentEvent(state, { type: "task_started" })).toBe(state);
  });
});
