import { describe, expect, it } from "vitest";

import {
  feedMessageToChatMessage,
  parseMessagePage,
  type FeedMessage,
} from "./messages-feed";

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

describe("历史分页游标", () => {
  it("取更旧一页的游标是该页最旧的 seq，下一页不会与当前页重叠", () => {
    const page = parseMessagePage({
      data: [
        { ...row, seq: 11, content: "十一" },
        { ...row, seq: 12, content: "十二" },
      ],
      has_more: true,
      // 旧网关返回的是本页最后一条的 seq；前端必须按"更旧"方向取最小值。
      next_cursor: "12",
    });

    expect(page.nextBeforeSeq).toBe(11);
    expect(page.messages.map((message) => message.seq)).toEqual([11, 12]);
  });
});

describe("账本消息的展示元数据", () => {
  it("历史消息保留 additional_kwargs，引用才能跨刷新显示", () => {
    const message = feedMessageToChatMessage(row);

    expect(message?.additional_kwargs).toEqual({
      knowledge_citations: [{ citation_id: "k1234abcd", filename: "manual.md" }],
    });
    expect(message?.response_metadata).toEqual({ model_name: "m" });
  });
});
