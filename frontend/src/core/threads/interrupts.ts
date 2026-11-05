/**
 * LangGraph HITL interrupt -> the small view model the chat panel renders.
 *
 * The gateway serializes LangGraph's Interrupt dataclass as
 * {"id": "...", "value": {...}}; the SDK may additionally normalize the
 * HITL payload to camelCase. Accept both spellings so a wire-format change
 * cannot make the confirmation panel disappear.
 */

export interface ChatInterrupt {
  /** Stable interrupt id when the server provides one. */
  id?: string;
  toolName: string;
  description: string;
  allowedDecisions: readonly string[];
  /** Number of action requests in this interrupt. */
  actionCount: number;
}

type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function array(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function parseChatInterrupt(
  interrupts: readonly { id?: string; value?: unknown }[] | undefined | null,
): ChatInterrupt | null {
  for (const interrupt of interrupts ?? []) {
    const value = record(interrupt.value);
    if (!value) continue;
    const actions = array(value.actionRequests ?? value.action_requests);
    if (actions.length === 0) continue;
    const first = record(actions[0]);
    if (!first) continue;

    const configs = array(value.reviewConfigs ?? value.review_configs);
    const firstConfig = record(configs[0]);
    const allowed = array(firstConfig?.allowedDecisions ?? firstConfig?.allowed_decisions)
      .filter((item): item is string => typeof item === "string");
    const decisions = allowed.length ? allowed : ["approve", "reject"];

    return {
      id: interrupt.id,
      toolName: text(first.name) || "工具",
      description: text(first.description) || "模型请求执行需要你确认的操作。",
      allowedDecisions: decisions,
      actionCount: actions.length,
    };
  }
  return null;
}

export function buildResumeCommand(
  interrupt: ChatInterrupt,
  decision: "approve" | "reject",
): { resume: { decisions: Array<{ type: "approve" | "reject"; message?: string }> } } {
  const item =
    decision === "reject"
      ? { type: "reject" as const, message: "用户拒绝了这次画像更新。" }
      : { type: "approve" as const };
  return {
    resume: {
      decisions: Array.from({ length: Math.max(1, interrupt.actionCount) }, () => ({ ...item })),
    },
  };
}
