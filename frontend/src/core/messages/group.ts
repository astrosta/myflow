import type { ChatMessage, DelegationEntry } from "../types";
import {
  buildSubagentView,
  TASK_TOOL_NAME,
  type SubagentLiveState,
} from "./subagent";
import { indexToolResults, resolveToolCalls, textOf, type ToolCallView } from "./tool-results";

/**
 * 消息分组（M5 §6.1 / §6.2）。
 *
 * 两个必须同时成立的约束：
 *
 * 1. **一条 AI 消息可以同时有用户可见的文本和 `tool_calls`**（"这个方法失败了，
 *    我换一种"）。把它当成纯工具元数据处理会丢掉这些文本。
 *
 * 2. **同一条消息在生命周期里会被两个不同的组件渲染**（流式期间是"处理中"的分组，
 *    结束后是"已完成"的气泡）。如果两边对"思考折叠放在文本上面还是下面"的处理
 *    不一致，用户会看到它们在这一轮结束的瞬间交换位置。
 *
 * 第 2 条的解法是结构性的：两个渲染器消费的是**同一份 `blocks` 数组**，
 * 顺序在数据层就定死了，组件没有机会各排各的。
 */

export type ContentBlockView = ThinkingBlockView | TextBlockView | ToolCallsBlockView;

export interface ThinkingBlockView {
  kind: "thinking";
  id: string;
  text: string;
}

export interface TextBlockView {
  kind: "text";
  id: string;
  text: string;
}

export interface ToolCallsBlockView {
  kind: "tool_calls";
  id: string;
  calls: ToolCallView[];
}

export interface StepView {
  id: string;
  message: ChatMessage;
  /** 顺序即渲染顺序。思考在它产出的答案之上，之前的文本保持原位 */
  blocks: ContentBlockView[];
  toolCalls: ToolCallView[];
}

export interface HumanGroup {
  kind: "human";
  id: string;
  message: ChatMessage;
}

export interface StepsGroup {
  kind: "steps";
  id: string;
  runId: string | null;
  steps: StepView[];
  /** 这一轮还在流。纯内容 AI 消息因此留在"处理中"，不外提成气泡 */
  streaming: boolean;
  /**
   * run 级别展示（运行时长、工作区变更卡片）的锚点。
   * §6.3：**不同的展示，锚点的候选集合可以不一样**——运行时长由列表组件在
   * 每个分组周围发出，接受任何分组类型；变更卡片由单条消息组件渲染，只接受 assistant。
   */
  anchorMessageId: string;
}

export interface AnswerGroup {
  kind: "answer";
  id: string;
  runId: string | null;
  message: ChatMessage;
  blocks: ContentBlockView[];
  anchorMessageId: string;
}

export type MessageGroup = HumanGroup | StepsGroup | AnswerGroup;

const REASONING_KEYS = [
  "reasoning_content",
  "reasoning",
  "thinking",
  "thinking_content",
] as const;

/** 从几个已知的落点里找思考文本。DeepSeek 走 `additional_kwargs.reasoning_content` */
export function reasoningText(message: ChatMessage): string {
  for (const bag of [message.additional_kwargs, message.response_metadata]) {
    if (!bag) continue;
    for (const key of REASONING_KEYS) {
      const value = bag[key];
      if (typeof value === "string" && value.trim()) return value;
    }
  }
  return "";
}

function isThinkingBlockType(type: string): boolean {
  return type === "thinking" || type === "reasoning" || type === "reasoning_content";
}

/**
 * 把一条消息拆成按序渲染的块。
 *
 * 规则：**思考在上，它产出的答案在下。在这段思考之前发出的文本保持原位。**
 * 用"保留 content 数组的原始顺序"来实现，而不是"把思考统一提到最前"——
 * 后者会让思考之前那段文本在渲染时被搬到思考后面。
 */
export function buildContentBlocks(
  message: ChatMessage,
  toolCalls: readonly ToolCallView[] = [],
): ContentBlockView[] {
  const blocks: ContentBlockView[] = [];
  let sawThinking = false;

  const pushThinking = (text: string) => {
    if (!text.trim()) return;
    sawThinking = true;
    blocks.push({ kind: "thinking", id: `${message.id}-think-${blocks.length}`, text });
  };

  const content = message.content;
  if (typeof content === "string") {
    // 纯字符串正文：思考块来自 additional_kwargs，它一定发生在正文之前
    const reasoning = reasoningText(message);
    if (reasoning) pushThinking(reasoning);
    if (content) blocks.push({ kind: "text", id: `${message.id}-text-0`, text: content });
  } else if (Array.isArray(content)) {
    content.forEach((block, index) => {
      const type = typeof block?.type === "string" ? block.type : "";
      if (isThinkingBlockType(type)) {
        const text =
          typeof block.text === "string"
            ? block.text
            : typeof block.thinking === "string"
              ? block.thinking
              : "";
        pushThinking(text);
        return;
      }
      if (type === "text" || type === "output_text" || typeof block?.text === "string") {
        const text = typeof block?.text === "string" ? block.text : "";
        if (text) blocks.push({ kind: "text", id: `${message.id}-text-${index}`, text });
        return;
      }
      // tool_use 之类的块由下面的 tool_calls 统一渲染，避免同一批调用被画两遍
    });
    if (!sawThinking) {
      const reasoning = reasoningText(message);
      if (reasoning) blocks.unshift({ kind: "thinking", id: `${message.id}-think-0`, text: reasoning });
    }
  } else {
    const reasoning = reasoningText(message);
    if (reasoning) pushThinking(reasoning);
  }

  if (toolCalls.length) {
    blocks.push({ kind: "tool_calls", id: `${message.id}-tools`, calls: [...toolCalls] });
  }

  return blocks;
}

export function hasToolCalls(message: ChatMessage): boolean {
  return (message.tool_calls?.length ?? 0) > 0;
}

/** 一条 AI 消息会不会被渲染成任何东西。空的占位消息不该产生分组 */
function isRenderable(message: ChatMessage): boolean {
  if (message.hidden) return false;
  if (message.type === "system") return false;
  if (message.type === "ai" && !hasToolCalls(message)) {
    return Boolean(textOf(message).trim() || reasoningText(message).trim());
  }
  return true;
}

export interface BuildGroupsInput {
  /** 已经过 `mergeMessages` 排序的完整列表 */
  messages: readonly ChatMessage[];
  /** 这一轮还在流。只作用于**最后一轮**（最后一个人类消息之后） */
  streaming?: boolean;
  activeRunId?: string | null;
  /** ThreadState 里持久化的委派台账；刷新页面后靠它恢复子任务卡片 */
  delegations?: readonly DelegationEntry[];
  /** 当前页面收到的 task_* custom 事件，按 tool_call_id 索引 */
  subagentEvents?: Readonly<Record<string, SubagentLiveState>>;
}

export function buildMessageGroups({
  messages,
  streaming = false,
  activeRunId = null,
  delegations = [],
  subagentEvents = {},
}: BuildGroupsInput): MessageGroup[] {
  const results = indexToolResults(messages);
  const delegationById = new Map<string, DelegationEntry>();
  for (const entry of delegations) {
    if (entry && typeof entry.id === "string" && entry.id) {
      delegationById.set(entry.id, entry);
    }
  }
  const withSubagents = (calls: readonly ToolCallView[]): ToolCallView[] =>
    calls.map((call) =>
      call.name === TASK_TOOL_NAME
        ? {
            ...call,
            subagent: buildSubagentView(call, {
              delegation: delegationById.get(call.id),
              live: subagentEvents[call.id],
            }),
          }
        : call,
    );
  const groups: MessageGroup[] = [];

  // 按人类消息切轮；人类消息之前的孤儿步骤归入"首轮"
  const turns: { human?: ChatMessage; items: ChatMessage[] }[] = [{ items: [] }];
  for (const message of messages) {
    if (message.type === "human" && !message.hidden) {
      turns.push({ human: message, items: [] });
      continue;
    }
    turns.at(-1)!.items.push(message);
  }
  // 丢掉空的首轮（大多数会话以人类消息开头）
  if (!turns[0]!.human && turns[0]!.items.length === 0) turns.shift();

  const lastTurnIndex = turns.length - 1;

  turns.forEach((turn, turnIndex) => {
    const isStreamingTurn = streaming && turnIndex === lastTurnIndex;

    if (turn.human) {
      groups.push({ kind: "human", id: `human-${turn.human.id}`, message: turn.human });
    }

    let pendingSteps: StepView[] = [];
    let pendingAnchor: string | null = null;
    let pendingRunId: string | null = null;

    const flushSteps = () => {
      if (!pendingSteps.length) return;
      groups.push({
        kind: "steps",
        id: `steps-${pendingSteps[0]!.id}`,
        runId: pendingRunId,
        steps: pendingSteps,
        streaming: isStreamingTurn,
        anchorMessageId: pendingAnchor ?? pendingSteps[0]!.id,
      });
      pendingSteps = [];
      pendingAnchor = null;
      pendingRunId = null;
    };

    for (const message of turn.items) {
      if (message.type === "tool") continue; // 结果已经回填进 toolCall
      if (!isRenderable(message)) continue;

      const calls = withSubagents(resolveToolCalls(message, results));
      const step: StepView = {
        id: `step-${message.id}`,
        message,
        blocks: buildContentBlocks(message, calls),
        toolCalls: calls,
      };

      if (hasToolCalls(message)) {
        pendingSteps.push(step);
        pendingAnchor ??= message.id;
        pendingRunId ??= message.run_id ?? null;
        continue;
      }

      // 纯内容 AI 消息：**流式期间留在"处理中"分组里**，直到这一轮结束。
      // provider 可能稍后往同一条消息上追加工具调用块——过早把它归类成最终回复，
      // 会让那段文字在结束的瞬间跳进步骤面板（§6.1）。
      if (isStreamingTurn) {
        pendingSteps.push(step);
        pendingAnchor ??= message.id;
        pendingRunId ??= message.run_id ?? null;
        continue;
      }

      flushSteps();
      groups.push({
        kind: "answer",
        id: `answer-${message.id}`,
        runId: message.run_id ?? activeRunId,
        message,
        blocks: buildContentBlocks(message, calls),
        anchorMessageId: message.id,
      });
    }

    flushSteps();
  });

  return groups;
}

/**
 * §6.3：run 级别的展示只挂在一个锚点上，否则一次 run 里的每条 AI 消息
 * 都会渲染一份一模一样的运行时长。
 *
 * 注意两种展示的候选集**故意不同**：运行时长由列表组件在每个分组周围发出，
 * 所以它接受任何分组类型；变更卡片由单条消息组件渲染，所以它只接受 assistant。
 * 不要为了"统一"把两者合并。
 */
export function pickDurationAnchor(groups: readonly MessageGroup[]): string | null {
  for (let index = groups.length - 1; index >= 0; index -= 1) {
    const group = groups[index]!;
    if (group.kind === "human") continue;
    return group.anchorMessageId;
  }
  return null;
}

export function pickRunArtifactAnchor(groups: readonly MessageGroup[]): string | null {
  for (let index = groups.length - 1; index >= 0; index -= 1) {
    const group = groups[index]!;
    if (group.kind === "answer") return group.anchorMessageId;
    if (group.kind === "steps") return group.steps.at(-1)!.message.id;
  }
  return null;
}
