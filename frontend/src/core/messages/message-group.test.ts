import { describe, expect, it } from "vitest";

import type { ChatMessage } from "../types";
import {
  buildMessageGroups,
  pickDurationAnchor,
  pickRunArtifactAnchor,
  type MessageGroup,
} from "./group";

const human = (id: string): ChatMessage => ({ id, type: "human", content: id });
const ai = (id: string, over: Partial<ChatMessage> = {}): ChatMessage => ({
  id,
  type: "ai",
  content: id,
  ...over,
});
const tool = (id: string, callId: string, over: Partial<ChatMessage> = {}): ChatMessage => ({
  id,
  type: "tool",
  content: id,
  tool_call_id: callId,
  ...over,
});

const blockKinds = (blocks: readonly { kind: string }[]) => blocks.map((b) => b.kind);
const stepsOf = (groups: readonly MessageGroup[]) =>
  groups.filter((group) => group.kind === "steps").flatMap((group) => group.steps);
const answersOf = (groups: readonly MessageGroup[]) =>
  groups.filter((group) => group.kind === "answer");
/** 两个渲染器消费的是同一份 `blocks`——流式期在 steps 组里，结束后在 answer 气泡里 */
const blocksOf = (groups: readonly MessageGroup[]) => {
  const step = stepsOf(groups)[0];
  return step ? step.blocks : answersOf(groups)[0]!.blocks;
};

describe("带正文的工具调用消息（§6.1）", () => {
  it("文本和 tool_calls 同时存在时，文本不会被当成纯工具元数据丢掉", () => {
    const message = ai("a1", {
      content: "这个方法失败了，我换一种。",
      tool_calls: [{ id: "call-1", name: "read_file", args: { path: "a.py" } }],
    });

    const [step] = stepsOf(buildMessageGroups({ messages: [human("u1"), message] }));

    expect(blockKinds(step!.blocks)).toEqual(["text", "tool_calls"]);
    expect(step!.blocks[0]).toMatchObject({ kind: "text", text: "这个方法失败了，我换一种。" });
  });

  it("工具结果按 tool_call_id 回填到卡片上", () => {
    const messages = [
      human("u1"),
      ai("a1", { tool_calls: [{ id: "call-1", name: "read_file", args: { path: "a.py" } }] }),
      tool("t1", "call-1", { content: "文件内容", status: "success" }),
    ];

    const [step] = stepsOf(buildMessageGroups({ messages }));

    expect(step!.toolCalls).toHaveLength(1);
    expect(step!.toolCalls[0]).toMatchObject({
      id: "call-1",
      name: "read_file",
      status: "success",
      resultText: "文件内容",
    });
  });

  it("task 调用按 tool_call_id 挂上持久化台账，渲染成子任务而不是普通工具", () => {
    const messages = [
      human("u1"),
      ai("a1", {
        tool_calls: [
          {
            id: "call-task",
            name: "task",
            args: {
              description: "调研竞品",
              subagent_type: "researcher",
              prompt: "请调研竞品",
            },
          },
        ],
      }),
      tool("t1", "call-task", {
        content: "任务成功。\n结果：结论",
        status: "success",
        additional_kwargs: { subagent_status: "completed", subagent_result_brief: "结论" },
      }),
    ];

    const [step] = stepsOf(
      buildMessageGroups({
        messages,
        delegations: [
          {
            id: "call-task",
            description: "台账里的竞品调研",
            subagent_type: "researcher",
            status: "completed",
            created_at: "2026-11-09T00:00:00Z",
          },
        ],
      }),
    );

    expect(step!.toolCalls[0]!.name).toBe("task");
    expect(step!.toolCalls[0]!.subagent).toMatchObject({
      id: "call-task",
      description: "调研竞品",
      subagentType: "researcher",
      status: "completed",
      resultText: "结论",
      startedAt: "2026-11-09T00:00:00Z",
    });
  });

  it("普通工具不会拿到子任务视图", () => {
    const messages = [
      human("u1"),
      ai("a1", { tool_calls: [{ id: "call-1", name: "read_file", args: { path: "a.py" } }] }),
    ];

    const [step] = stepsOf(buildMessageGroups({ messages }));
    expect(step!.toolCalls[0]!.subagent).toBeUndefined();
  });

  it("还没有结果时状态是 pending", () => {
    const messages = [
      human("u1"),
      ai("a1", { tool_calls: [{ id: "call-1", name: "slow_tool", args: {} }] }),
    ];

    expect(stepsOf(buildMessageGroups({ messages, streaming: true }))[0]!.toolCalls[0]!.status).toBe(
      "pending",
    );
  });

  it("失败的结果标成 error", () => {
    const messages = [
      human("u1"),
      ai("a1", { tool_calls: [{ id: "call-1", name: "boom", args: {} }] }),
      tool("t1", "call-1", { content: "traceback", status: "error" }),
    ];

    expect(stepsOf(buildMessageGroups({ messages }))[0]!.toolCalls[0]!.status).toBe("error");
  });
});

describe("流式期间纯内容 AI 消息留在「处理中」分组（§6.1）", () => {
  const messages = [human("u1"), ai("a1", { content: "我想想" })];

  it("流式期间不外提成气泡——provider 可能稍后往同一条消息上追加工具调用块", () => {
    const groups = buildMessageGroups({ messages, streaming: true });

    expect(answersOf(groups)).toHaveLength(0);
    expect(stepsOf(groups).map((step) => step.message.id)).toEqual(["a1"]);
  });

  it("这一轮结束后才变成「已完成」的气泡", () => {
    const groups = buildMessageGroups({ messages, streaming: false });

    expect(answersOf(groups).map((group) => group.id)).toEqual(["answer-a1"]);
    expect(stepsOf(groups)).toHaveLength(0);
  });

  it("一次工具调用之后的纯内容消息同样保持可见", () => {
    const withTool = [
      human("u1"),
      ai("a1", { tool_calls: [{ id: "call-1", name: "read_file", args: {} }] }),
      tool("t1", "call-1"),
      ai("a2", { content: "看完之后的感想" }),
    ];

    const groups = buildMessageGroups({ messages: withTool, streaming: true });

    expect(answersOf(groups)).toHaveLength(0);
    expect(stepsOf(groups).map((step) => step.message.id)).toEqual(["a1", "a2"]);
  });

  it("流式期间那段文字不会跳进步骤面板之外的地方（它本来就在步骤面板里）", () => {
    const streaming = buildMessageGroups({ messages, streaming: true });
    const finished = buildMessageGroups({ messages, streaming: false });

    // 文字在两个阶段都在屏幕上，只是容器换了
    expect(stepsOf(streaming)).toHaveLength(1);
    expect(answersOf(finished)).toHaveLength(1);
  });
});

describe("思考在答案上面，两个渲染器都是（§6.2）", () => {
  it("思考块排在它产出的文本之前", () => {
    const message = ai("a1", {
      content: [
        { type: "thinking", text: "先算一下" },
        { type: "text", text: "答案是 42" },
      ],
    });

    expect(
      blockKinds(blocksOf(buildMessageGroups({ messages: [human("u1"), message] }))),
    ).toEqual(["thinking", "text"]);
  });

  it("**在这段思考之前发出的文本保持原位**", () => {
    const message = ai("a1", {
      content: [
        { type: "text", text: "先说的话" },
        { type: "thinking", text: "然后想了想" },
        { type: "text", text: "结论" },
      ],
    });

    const blocks = blocksOf(buildMessageGroups({ messages: [human("u1"), message] }));

    expect(blocks.map((block) => (block.kind === "text" ? block.text : block.kind))).toEqual([
      "先说的话",
      "thinking",
      "结论",
    ]);
  });

  it("思考来自 additional_kwargs.reasoning_content 时也排在正文之上", () => {
    const message = ai("a1", {
      content: "最终答案",
      additional_kwargs: { reasoning_content: "DeepSeek 的思考落点" },
    });

    expect(
      blockKinds(blocksOf(buildMessageGroups({ messages: [human("u1"), message] }))),
    ).toEqual(["thinking", "text"]);
  });

  /**
   * 这一条是 §6.2 的核心：同一条消息在流式期间由"处理中"分组渲染，
   * 结束后由"已完成"气泡渲染。两边的块顺序必须一致，
   * 否则用户会看到它们在这一轮结束的瞬间交换位置。
   */
  it("流式分组和终态气泡消费同一份 blocks，顺序不会在结束瞬间交换", () => {
    const message = ai("a1", {
      content: [
        { type: "thinking", text: "推理过程" },
        { type: "text", text: "答案文本" },
      ],
      seq: 3,
    });

    const streaming = stepsOf(buildMessageGroups({ messages: [human("u1"), message], streaming: true }));
    const finished = answersOf(buildMessageGroups({ messages: [human("u1"), message], streaming: false }));

    const streamingKinds = blockKinds(streaming[0]!.blocks);
    const finishedKinds = blockKinds(finished[0]!.blocks);

    expect(streamingKinds).toEqual(["thinking", "text"]);
    expect(finishedKinds).toEqual(streamingKinds);
    expect(finished[0]!.blocks[0]).toEqual(streaming[0]!.blocks[0]);
  });
});

describe("run 级别的展示只挂在一个锚点上（§6.3）", () => {
  const messages = [
    human("u1"),
    ai("a1", { run_id: "run-1", tool_calls: [{ id: "c1", name: "t", args: {} }] }),
    tool("t1", "c1", { run_id: "run-1" }),
    ai("a2", { run_id: "run-1", content: "第一段答案" }),
    ai("a3", { run_id: "run-1", content: "第二段答案" }),
  ];

  it("一次 run 以多个终态 assistant 气泡结束", () => {
    const answers = answersOf(buildMessageGroups({ messages, streaming: false }));

    expect(answers.map((group) => group.id)).toEqual(["answer-a2", "answer-a3"]);
  });

  it("运行时长只解析出一个锚点，不会每条 AI 消息都渲染一份", () => {
    const groups = buildMessageGroups({ messages, streaming: false });
    const anchors = groups
      .filter((group) => group.kind !== "human")
      .map((group) => group.anchorMessageId);

    expect(pickDurationAnchor(groups)).toBe(anchors.at(-1));
    expect(new Set(anchors).size).toBe(anchors.length);
  });

  it("两种展示的候选集故意不同：时长接受任何分组，变更卡片只接受 assistant", () => {
    const onlySteps = [human("u1"), ai("a1", { tool_calls: [{ id: "c1", name: "t", args: {} }] })];
    const stepGroups = buildMessageGroups({ messages: onlySteps, streaming: true });

    expect(pickDurationAnchor(stepGroups)).toBe("a1");
    // 变更卡片由单条消息组件渲染，它只认 assistant —— 这里本该是 assistant 的消息
    // 还在"处理中"分组里，所以没有候选
    expect(pickRunArtifactAnchor(stepGroups)).toBe("a1");
    expect(pickRunArtifactAnchor(buildMessageGroups({ messages: onlySteps, streaming: false }))).toBe(
      "a1",
    );
  });

  it("没有可锚定的分组时返回 null", () => {
    expect(pickDurationAnchor(buildMessageGroups({ messages: [human("u1")] }))).toBeNull();
    expect(pickRunArtifactAnchor([])).toBeNull();
  });
});

describe("分组不产出噪声", () => {
  it("隐藏的控制消息不产生任何分组", () => {
    const groups = buildMessageGroups({
      messages: [human("u1"), ai("control", { hidden: true }), ai("a1", { content: "答案" })],
    });

    expect(groups.map((group) => group.kind)).toEqual(["human", "answer"]);
  });

  it("空的 AI 占位消息不产生分组", () => {
    const groups = buildMessageGroups({ messages: [human("u1"), ai("a1", { content: "" })] });

    expect(groups.map((group) => group.kind)).toEqual(["human"]);
  });

  it("没有人类消息时，开头的步骤归入一个分组", () => {
    const groups = buildMessageGroups({
      messages: [ai("a1", { tool_calls: [{ id: "c1", name: "t", args: {} }] })],
    });

    expect(groups.map((group) => group.kind)).toEqual(["steps"]);
  });
});
