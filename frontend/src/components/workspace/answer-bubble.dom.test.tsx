import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { AnswerGroup } from "@/core/messages/group";

import { AnswerBubble } from "./answer-bubble";

/**
 * 终态助手气泡的形制：**不写"助手"、不套外框**——角色已经由左右布局区分，
 * 再套一层矩形框只会把正文挤窄。
 */

afterEach(cleanup);

function group(createdAt: string | null = null): AnswerGroup {
  return {
    kind: "answer",
    id: "answer-1",
    message: {
      id: "m-1",
      type: "ai",
      content: "答案正文",
      created_at: createdAt,
    },
    blocks: [{ kind: "text", id: "m-1-text-0", text: "答案正文" }],
    runId: null,
    anchorMessageId: "m-1",
  };
}

describe("助手气泡", () => {
  it("不显示“助手”两个字", () => {
    const view = render(<AnswerBubble group={group()} />);
    expect(view.queryByText("助手")).toBeNull();
    expect(view.getByText("答案正文")).toBeTruthy();
  });

  it("没有外层矩形框（边框、背景、圆角都不挂）", () => {
    const view = render(<AnswerBubble group={group()} />);
    const article = view.getByLabelText("助手回复");
    const classes = article.className;

    expect(classes).not.toContain("border");
    expect(classes).not.toContain("rounded");
    expect(classes).not.toContain("bg-white");
    expect(classes).toContain("w-full");
  });

  it("有时间戳就单独一行小字，没有就不渲染", () => {
    const timed = render(<AnswerBubble group={group("2026-10-23T10:00:00Z")} />);
    expect(timed.getByText(/\d{2}:\d{2}/)).toBeTruthy();
    cleanup();

    const untimed = render(<AnswerBubble group={group()} />);
    expect(untimed.queryByText(/\d{2}:\d{2}/)).toBeNull();
  });
});
