import { describe, expect, it } from "vitest";

import { feedMessageToChatMessage, type FeedMessage } from "./messages-feed";

const row: FeedMessage = {
  thread_id: "t1",
  run_id: "r1",
  seq: 7,
  event_type: "llm.ai.response",
  content: {
    id: "m-1",
    type: "ai",
    content: "答案",
    additional_kwargs: {
      knowledge_citations: [{ citation_id: "k1234abcd", filename: "manual.md" }],
    },
    response_metadata: { model_name: "m" },
  },
  metadata: {},
  created_at: null,
};

describe("账本消息的展示元数据", () => {
  it("历史消息保留 additional_kwargs，引用才能跨刷新显示", () => {
    const message = feedMessageToChatMessage(row);

    expect(message?.additional_kwargs).toEqual({
      knowledge_citations: [{ citation_id: "k1234abcd", filename: "manual.md" }],
    });
    expect(message?.response_metadata).toEqual({ model_name: "m" });
  });
});
