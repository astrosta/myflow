import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { SubagentView } from "@/core/messages/subagent";
import type { ToolCallView } from "@/core/messages/tool-results";

import { ContentBlocks } from "./content-blocks";
import { SubagentCard } from "./subagent-card";

afterEach(cleanup);

function makeView(over: Partial<SubagentView> = {}): SubagentView {
  return {
    id: "call-task",
    description: "调研竞品",
    prompt: "请调研三个竞品，并给出结论。",
    subagentType: "researcher",
    status: "completed",
    statusLabel: "已完成",
    modelName: "deepseek-chat",
    usage: { input_tokens: 10, output_tokens: 5, total_tokens: 15 },
    steps: [],
    resultText: "竞品结论",
    ...over,
  };
}

function makeCall(over: Partial<ToolCallView> = {}): ToolCallView {
  return {
    id: "call-task",
    name: "task",
    args: { description: "调研竞品" },
    status: "success",
    resultText: "任务成功。\n结果：竞品结论",
    subagent: makeView(),
    ...over,
  };
}

describe("子任务卡片", () => {
  it("折叠时直接读出描述、子代理类型和状态，结论不提前铺开", () => {
    render(<SubagentCard view={makeView()} />);

    expect(screen.getByRole("region", { name: /子任务 调研竞品/ })).toBeTruthy();
    const trigger = screen.getByRole("button", { name: /调研竞品/ });
    expect(trigger.textContent).toContain("researcher");
    expect(trigger.textContent).toContain("已完成");
    expect(trigger.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText("竞品结论")).toBeNull();
  });

  it("展开后显示步骤、最终结论、模型和用量", () => {
    render(
      <SubagentCard
        view={makeView({
          steps: [
            {
              key: "id:step-1",
              index: 1,
              kind: "tool",
              name: "web_search",
              text: "搜索竞品资料",
              toolNames: [],
            },
            {
              key: "id:step-2",
              index: 2,
              kind: "ai",
              text: "整理成对比表",
              toolNames: [],
            },
          ],
        })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /调研竞品/ }));

    expect(screen.getByText("执行步骤")).toBeTruthy();
    expect(screen.getByText("搜索竞品资料")).toBeTruthy();
    expect(screen.getByText("整理成对比表")).toBeTruthy();
    expect(screen.getByText("最终结论")).toBeTruthy();
    expect(screen.getByText("竞品结论")).toBeTruthy();
    expect(screen.getByText(/deepseek-chat/)).toBeTruthy();
    expect(screen.getByText(/15 tokens/)).toBeTruthy();
    expect(screen.getByText(/请调研三个竞品/)).toBeTruthy();
  });

  it("运行中显示进度提示，失败显示原因而不是成功结论", () => {
    const view = makeView({
      status: "running",
      statusLabel: "运行中",
      resultText: "",
      steps: [],
    });
    render(<SubagentCard view={view} />);
    expect(screen.getByRole("button", { name: /运行中/ })).toBeTruthy();

    cleanup();
    render(
      <SubagentCard
        view={makeView({
          status: "failed",
          statusLabel: "失败",
          resultText: "",
          error: "子代理执行失败",
        })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /失败/ }));
    expect(screen.getByText("失败原因")).toBeTruthy();
    expect(screen.getByText("子代理执行失败")).toBeTruthy();
    expect(screen.queryByText("最终结论")).toBeNull();
  });

  it("task 调用走子任务卡片，不再渲染成普通工具行", () => {
    render(
      <ContentBlocks
        blocks={[{ kind: "tool_calls", id: "block-1", calls: [makeCall()] }]}
      />,
    );

    expect(screen.getByRole("region", { name: /子任务 调研竞品/ })).toBeTruthy();
    expect(screen.queryByLabelText("工具调用 task")).toBeNull();
    expect(screen.queryByRole("button", { name: /工具 task/ })).toBeNull();
  });
});
