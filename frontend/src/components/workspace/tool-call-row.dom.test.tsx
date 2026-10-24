import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { ToolCallView } from "@/core/messages/tool-results";

import { ToolCallRow, toolCallSummary } from "./tool-call-row";

/**
 * 工具调用行：和思考行（`thinking-block.tsx`）同一形制——
 * 一行 `工具 · 工具名 · 参数摘要`，点整行展开参数与结果。
 */

afterEach(cleanup);

function makeCall(over: Partial<ToolCallView> = {}): ToolCallView {
  return {
    id: "call-1",
    name: "write_file",
    args: { path: "notes.txt", content: "hello" },
    status: "success",
    resultText: "wrote 11 bytes to notes.txt",
    ...over,
  };
}

describe("工具行", () => {
  it("折叠时一行读完：标题「工具」+ 工具名 + 参数摘要", () => {
    const view = render(<ToolCallRow call={makeCall()} />);
    const row = view.getByRole("button", { name: /工具 write_file/ });

    expect(row.textContent).toContain("工具");
    expect(row.textContent).toContain("write_file");
    // 摘要把 `path` 挑出来（正文里的 content 不进摘要，否则一行放不下）
    expect(row.textContent).toContain("notes.txt");
    expect(row.getAttribute("aria-expanded")).toBe("false");
  });

  it("参数和结果默认不挂载，点开都在（§9 验收清单第一条）", () => {
    const view = render(<ToolCallRow call={makeCall()} />);
    expect(view.queryByText("参数")).toBeNull();
    expect(view.queryByText(/wrote 11 bytes/)).toBeNull();

    fireEvent.click(view.getByRole("button", { name: /工具 write_file/ }));

    expect(view.getByText("参数")).toBeTruthy();
    expect(view.getByText("结果")).toBeTruthy();
    expect(view.getByText(/hello/)).toBeTruthy();
    expect(view.getByText(/wrote 11 bytes/)).toBeTruthy();
    expect(view.getByRole("button", { name: /工具 write_file/ }).getAttribute("aria-expanded")).toBe("true");
  });

  it("结果还没回来：行内状态是运行中，展开写「等待结果…」", () => {
    const view = render(<ToolCallRow call={makeCall({ status: "pending", resultText: "" })} />);

    // 读屏靠 aria-live 播报，视觉上靠图标呼吸
    expect(view.getByText("工具运行中")).toBeTruthy();
    expect(view.getByRole("button", { name: /运行中/ })).toBeTruthy();

    fireEvent.click(view.getByRole("button", { name: /运行中/ }));
    expect(view.getByText("等待结果…")).toBeTruthy();
  });

  it("失败的行尾标红「失败」，不用展开就知道", () => {
    const view = render(<ToolCallRow call={makeCall({ status: "error", resultText: "boom" })} />);

    expect(view.getByRole("button", { name: /失败/ })).toBeTruthy();
    expect(view.getByText("失败")).toBeTruthy();
  });

  it("流式半截 JSON 也能给出摘要：取最后一个字符串值", () => {
    const view = render(
      <ToolCallRow
        call={makeCall({ args: {}, argsText: '{"query": "北京天', status: "pending", resultText: "" })}
      />,
    );

    expect(view.getByRole("button", { name: /工具 write_file/ }).textContent).toContain("北京天");
  });
});

describe("toolCallSummary", () => {
  it("优先取 command / query / path 这类主语参数", () => {
    expect(toolCallSummary(makeCall({ name: "bash", args: { command: "ls -la" } }))).toBe("bash · ls -la");
    expect(toolCallSummary(makeCall({ name: "search", args: { query: "北京天气" } }))).toBe(
      "search · 北京天气",
    );
  });

  it("没有任何可用参数时只留工具名，不留一个悬空的「·」", () => {
    expect(toolCallSummary(makeCall({ name: "todo", args: {} }))).toBe("todo");
  });

  it("多行参数压成一行，太长就截断", () => {
    const summary = toolCallSummary(makeCall({ name: "bash", args: { command: "a\nb\nc" } }));
    expect(summary).toBe("bash · a b c");

    const long = toolCallSummary(makeCall({ name: "bash", args: { command: "x".repeat(200) } }));
    expect(long.endsWith("…")).toBe(true);
    expect(long.length).toBeLessThan(120);
  });
});
