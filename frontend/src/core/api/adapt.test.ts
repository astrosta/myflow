import { describe, expect, it } from "vitest";

import { toChatMessage } from "./adapt";

describe("stream message 的隐藏标记", () => {
  it("hide_from_ui 的注入消息标成 hidden，不进入消息列表", () => {
    const message = toChatMessage(
      {
        id: "injected",
        type: "human",
        content: "<durable_context_data>...</durable_context_data>",
        additional_kwargs: { hide_from_ui: true, durable_context_data: true },
      },
      0,
    );

    expect(message.hidden).toBe(true);
  });

  it("真实用户消息不加隐藏标记", () => {
    const message = toChatMessage(
      { id: "u1", type: "human", content: "问题" },
      0,
    );

    expect(message.hidden).toBeUndefined();
  });
});
