import type { ChatMessage, DelegationEntry } from "../types";

/**
 * task 工具的前端视图。
 *
 * 协议上它仍是一次普通工具调用：AI 消息发 task 调用，TaskNode 返回 ToolMessage。
 * 但展示层要把它当成「子任务」卡片：description 是给用户看的标签，进度来自
 * task_started/task_running/task_completed... custom 帧，结论来自工具结果或台账。
 *
 * 刷新页面时 live 事件已经没了，卡片由两条持久数据重建：
 *   - ThreadState.delegations（按 tool_call_id 关联）
 *   - 历史里的 task ToolMessage（additional_kwargs 带终态元数据）
 */

export const TASK_TOOL_NAME = "task";

export type SubagentStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "timed_out"
  | "polling_timed_out";

const TERMINAL_STATUSES: ReadonlySet<SubagentStatus> = new Set([
  "completed",
  "failed",
  "cancelled",
  "timed_out",
  "polling_timed_out",
]);

const STATUS_LABELS: Record<SubagentStatus, string> = {
  pending: "等待开始",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  timed_out: "已超时",
  polling_timed_out: "等待超时",
};

const STATUS_BY_EVENT: Record<string, SubagentStatus> = {
  task_started: "running",
  task_completed: "completed",
  task_failed: "failed",
  task_cancelled: "cancelled",
  task_timed_out: "timed_out",
};

export interface SubagentUsage {
  input_tokens?: number;
  output_tokens?: number;
  total_tokens?: number;
}

export interface SubagentStep {
  /** 去重键：事件可能因重连/重放重复到达 */
  key: string;
  /** 子代理自己的消息下标，用于稳定排序 */
  index: number;
  kind: "ai" | "tool" | "other";
  name?: string;
  text: string;
  toolNames: string[];
}

/** 一次任务从 custom 事件汇总出来的实时状态（只在当前页面生命周期内）。 */
export interface SubagentLiveState {
  taskId: string;
  description?: string;
  subagentType?: string;
  modelName?: string;
  status?: SubagentStatus;
  stopReason?: string;
  usage?: SubagentUsage;
  result?: string;
  error?: string;
  steps: SubagentStep[];
}

export interface SubagentInput {
  id: string;
  name: string;
  args: Record<string, unknown>;
  status: "pending" | "success" | "error";
  result?: ChatMessage;
  resultText: string;
}

export interface SubagentEvidence {
  delegation?: DelegationEntry;
  live?: SubagentLiveState;
}

export interface SubagentView {
  id: string;
  description: string;
  prompt: string;
  subagentType: string;
  status: SubagentStatus;
  statusLabel: string;
  modelName?: string;
  stopReason?: string;
  usage?: SubagentUsage;
  steps: SubagentStep[];
  resultText: string;
  error?: string;
  startedAt?: string;
}

export function subagentStatusLabel(status: SubagentStatus): string {
  return STATUS_LABELS[status];
}

/** 未知值不猜成功；调用方再按工具结果/调用状态兜底。 */
export function normalizeSubagentStatus(value: unknown): SubagentStatus | undefined {
  if (value === "in_progress") return "running";
  if (typeof value !== "string") return undefined;
  return Object.hasOwn(STATUS_LABELS, value) ? (value as SubagentStatus) : undefined;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const textField = (value: unknown): string =>
  typeof value === "string" ? value.trim() : "";

function numberField(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

export function readSubagentUsage(value: unknown): SubagentUsage | undefined {
  if (!isRecord(value)) return undefined;
  const usage: SubagentUsage = {};
  const input = numberField(value.input_tokens);
  const output = numberField(value.output_tokens);
  const total = numberField(value.total_tokens);
  if (input !== undefined) usage.input_tokens = input;
  if (output !== undefined) usage.output_tokens = output;
  if (total !== undefined) usage.total_tokens = total;
  return input === undefined && output === undefined && total === undefined ? undefined : usage;
}

function firstLine(text: string, limit = 96): string {
  const line = text.split(/\r?\n/, 1)[0]?.replace(/\s+/g, " ").trim() ?? "";
  return line.length <= limit ? line : line.slice(0, limit) + "…";
}

function stepFromMessage(
  message: Record<string, unknown>,
  rawIndex: unknown,
  fallbackIndex: number,
): SubagentStep | null {
  const kindValue = textField(message.type);
  const kind: SubagentStep["kind"] =
    kindValue === "ai" || kindValue === "tool" ? kindValue : "other";
  const name = textField(message.name) || undefined;
  // 步骤正文放进 <pre>，保留缩进和换行；只用 trim() 判断是否为空。
  const text = typeof message.content === "string" ? message.content : "";
  const toolNames = Array.isArray(message.tool_calls)
    ? message.tool_calls
        .map((call) => (isRecord(call) ? textField(call.name) : ""))
        .filter(Boolean)
    : [];
  if (!text.trim() && !name && toolNames.length === 0) return null;

  const index = numberField(rawIndex) ?? fallbackIndex;
  const id = textField(message.id);
  const key = id
    ? "id:" + id
    : ["idx", String(index), kind, text.slice(0, 80), toolNames.join(",")].join(":");
  return { key, index, kind, ...(name ? { name } : {}), text, toolNames };
}

/**
 * 合并一条 root namespace 的 task_* custom 事件。
 *
 * 终态不会被迟到的 started/running 复活；重复步骤按消息 id 去重。
 * 非 task 事件、缺 task_id 的事件原样返回同一个对象，避免触发无意义重渲染。
 */
export function applySubagentEvent(
  state: Record<string, SubagentLiveState>,
  data: unknown,
): Record<string, SubagentLiveState> {
  if (!isRecord(data)) return state;
  const type = textField(data.type);
  const taskId = textField(data.task_id);
  if (
    !taskId ||
    !(Object.hasOwn(STATUS_BY_EVENT, type) || type === "task_running")
  ) {
    return state;
  }

  const previous = state[taskId];
  const previousTerminal = previous?.status !== undefined && TERMINAL_STATUSES.has(previous.status);
  const next: SubagentLiveState = {
    taskId,
    ...(previous ?? {}),
    steps: previous?.steps ? [...previous.steps] : [],
  };

  const description = textField(data.description);
  if (description) next.description = description;
  const subagentType = textField(data.subagent_type);
  if (subagentType) next.subagentType = subagentType;
  const modelName = textField(data.model_name);
  if (modelName) next.modelName = modelName;

  if (type === "task_running") {
    if (!previousTerminal && previous?.status === undefined) next.status = "running";
    const usage = readSubagentUsage(data.usage);
    if (usage) next.usage = usage;
    const message = isRecord(data.message) ? data.message : null;
    if (message && !previousTerminal) {
      const fallbackIndex = (next.steps.at(-1)?.index ?? -1) + 1;
      const step = stepFromMessage(message, data.message_index, fallbackIndex);
      if (step && !next.steps.some((item) => item.key === step.key)) {
        next.steps.push(step);
        // 长任务只保留最近 100 步：live 状态在内存里，不能让一次委派无限膨胀。
        if (next.steps.length > 100) next.steps = next.steps.slice(-100);
      }
    }
  } else {
    const status = STATUS_BY_EVENT[type]!;
    if (!previousTerminal || TERMINAL_STATUSES.has(status)) {
      next.status = normalizeSubagentStatus(data.status) ?? status;
    }
    const stopReason = textField(data.stop_reason);
    if (stopReason) next.stopReason = stopReason;
    const usage = readSubagentUsage(data.usage);
    if (usage) next.usage = usage;
    const result = textField(data.result);
    if (result) next.result = result;
    const error = textField(data.error);
    if (error) next.error = error;
  }

  return { ...state, [taskId]: next };
}

function resultMetadata(call: SubagentInput): Record<string, unknown> {
  return isRecord(call.result?.additional_kwargs) ? call.result!.additional_kwargs! : {};
}

function argText(args: Record<string, unknown>, key: string): string {
  return textField(args[key]);
}

function normalizeStepText(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

/** 步骤正文和最终结论文本是否只是同一段答案的两个副本。 */
function stepTextDuplicatesResult(stepText: string, resultText: string): boolean {
  const step = normalizeStepText(stepText);
  const result = normalizeStepText(resultText);
  if (!step || !result) return false;
  if (step === result || step.startsWith(result) || result.startsWith(step)) return true;
  // 工具结果正文可能带「任务成功。结果：……」的外壳；长答案被包在里面也算重复。
  if (step.length >= 40 && result.includes(step)) return true;
  // result_brief 是头尾截断（\n...\n）：比较首尾两段。
  const marker = result.indexOf("...");
  if (marker > 0) {
    const head = result.slice(0, marker).trim();
    const tail = result.slice(marker + 3).trim();
    if (head && tail && step.startsWith(head) && step.endsWith(tail)) return true;
  }
  return false;
}

/**
 * 组装一个子任务卡片视图。
 *
 * 状态优先级：
 *   1. 工具结果里的 subagent_status（终态权威）
 *   2. 实时 task_* 事件（刷新前最快）
 *   3. 持久化台账（刷新后）
 *   4. 工具调用本身的 pending/success/error
 *
 * 结果正文优先用台账 brief（2000 字）或工具结果 brief，最后才退回 ToolMessage 的散文。
 */
export function buildSubagentView(
  call: SubagentInput,
  evidence: SubagentEvidence = {},
): SubagentView {
  const { delegation, live } = evidence;
  const meta = resultMetadata(call);
  const resultStatus = normalizeSubagentStatus(meta.subagent_status);
  const delegationStatus = normalizeSubagentStatus(delegation?.status);
  const hasResult = call.result !== undefined;
  const status: SubagentStatus = hasResult
    ? resultStatus ??
      delegationStatus ??
      (call.status === "error" ? "failed" : "completed")
    : live?.status ??
      delegationStatus ??
      (call.status === "pending"
        ? "running"
        : call.status === "error"
          ? "failed"
          : "completed");

  const prompt = argText(call.args, "prompt");
  const description =
    argText(call.args, "description") ||
    textField(delegation?.description) ||
    textField(live?.description) ||
    firstLine(prompt) ||
    "子任务";
  const subagentType =
    argText(call.args, "subagent_type") ||
    textField(delegation?.subagent_type) ||
    textField(live?.subagentType);

  const error =
    textField(meta.subagent_error) || textField(live?.error) || undefined;
  const rawResult =
    textField(meta.subagent_result_brief) ||
    textField(delegation?.result_brief) ||
    textField(live?.result);
  const resultText = rawResult || (error ? "" : call.resultText.trim());
  // 子代理的最后一条 AI 消息既是一个 step，也是它的最终结论。卡片里只保留
  // 「最终结论」那一份，避免展开后同一段文字出现两遍。
  const steps = (live?.steps ?? []).filter(
    (step) => !(step.kind === "ai" && stepTextDuplicatesResult(step.text, resultText)),
  );
  const modelName = textField(meta.subagent_model_name) || live?.modelName;
  const stopReason =
    textField(meta.subagent_stop_reason) || delegation?.stop_reason || live?.stopReason;
  const usage = readSubagentUsage(meta.subagent_token_usage) ?? live?.usage;

  return {
    id: call.id,
    description,
    prompt,
    subagentType,
    status,
    statusLabel: subagentStatusLabel(status),
    ...(modelName ? { modelName } : {}),
    ...(stopReason ? { stopReason } : {}),
    ...(usage ? { usage } : {}),
    steps,
    resultText,
    ...(error ? { error } : {}),
    ...(delegation?.created_at ? { startedAt: delegation.created_at } : {}),
  };
}
