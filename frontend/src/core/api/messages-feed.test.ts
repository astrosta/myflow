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

describe("历史工具结果的身份与隐藏消息", () => {
  it("tool_call_id 在消息正文里（不在 row.metadata）时也要回填", () => {
    const toolRow: FeedMessage = {
      ...row,
      event_type: "llm.tool.result",
      content: {
        id: "tool-1",
        type: "tool",
        name: "task",
        content: "任务成功。\n结果：结论",
        tool_call_id: "call-task-1",
        additional_kwargs: { subagent_status: "completed" },
      },
    };

    const message = feedMessageToChatMessage(toolRow);

    expect(message?.tool_call_id).toBe("call-task-1");
    expect(message?.name).toBe("task");
    expect(message?.additional_kwargs).toEqual({ subagent_status: "completed" });
  });

  it("hide_from_ui 的持久上下文不会被当成用户消息渲染", () => {
    const hiddenRow: FeedMessage = {
      ...row,
      seq: 12,
      event_type: "llm.human.input",
      content: {
        id: null,
        type: "human",
        content: "<durable_context_data>\n摘要\n</durable_context_data>",
        additional_kwargs: {
          hide_from_ui: true,
          durable_context_data: true,
          myflow_content_kind: "durable_context",
        },
      },
    };

    const message = feedMessageToChatMessage(hiddenRow);

    expect(message?.hidden).toBe(true);
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
