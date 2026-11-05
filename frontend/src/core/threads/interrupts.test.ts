import { describe, expect, it } from "vitest";

import { buildResumeCommand, parseChatInterrupt } from "./interrupts";

const hitlInterrupt = {
  id: "interrupt-1",
  value: {
    actionRequests: [
      {
        name: "update_user_profile",
        args: { fields: { "identity.preferred_name": "小林" } },
        description: "模型准备更新你的「我的画像」。",
      },
    ],
    reviewConfigs: [{ allowedDecisions: ["approve", "reject"] }],
  },
};

describe("parseChatInterrupt", () => {
  it("把 HITL action request 转成确认面板视图", () => {
    const parsed = parseChatInterrupt([hitlInterrupt]);

    expect(parsed).toEqual({
      id: "interrupt-1",
      toolName: "update_user_profile",
      description: "模型准备更新你的「我的画像」。",
      allowedDecisions: ["approve", "reject"],
      actionCount: 1,
    });
  });

  it("兼容 Python 服务端的 snake_case 字段", () => {
    const parsed = parseChatInterrupt([
      {
        id: "interrupt-2",
        value: {
          action_requests: [{ name: "update_user_profile", description: "确认" }],
          review_configs: [{ allowed_decisions: ["approve"] }],
        },
      },
    ]);

    expect(parsed?.allowedDecisions).toEqual(["approve"]);
    expect(parsed?.toolName).toBe("update_user_profile");
  });

  it("没有 action request 的 breakpoint 不渲染确认面板", () => {
    expect(parseChatInterrupt([{ id: "plain", value: {} }])).toBeNull();
    expect(parseChatInterrupt(undefined)).toBeNull();
  });
});

describe("buildResumeCommand", () => {
  it("approve 为每个 action 生成一条决策", () => {
    expect(
      buildResumeCommand(
        {
          toolName: "update_user_profile",
          description: "",
          allowedDecisions: ["approve", "reject"],
          actionCount: 2,
        },
        "approve",
      ),
    ).toEqual({
      resume: { decisions: [{ type: "approve" }, { type: "approve" }] },
    });
  });

  it("reject 带固定拒绝原因", () => {
    expect(
      buildResumeCommand(
        {
          toolName: "update_user_profile",
          description: "",
          allowedDecisions: ["approve", "reject"],
          actionCount: 1,
        },
        "reject",
      ),
    ).toEqual({
      resume: { decisions: [{ type: "reject", message: "用户拒绝了这次画像更新。" }] },
    });
  });
});
