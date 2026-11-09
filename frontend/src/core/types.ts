/**
 * 前端侧的 wire 类型。
 *
 * 这里刻意不依赖 `@langchain/langgraph-sdk` 的类型：SDK 的 BaseMessage 是
 * provider 形状的，而 UI 需要的是"带服务端展示元数据的消息"。两者在边界
 * （`core/api`）上转换一次，core/ 里剩下的全是纯函数，可以用 node 环境单测。
 */

export type MessageType = "human" | "ai" | "tool" | "system";

export interface ToolCall {
  id?: string;
  name?: string;
  args?: Record<string, unknown>;
  /** 流式期间的半截 JSON。解析失败时保住原文，别丢给 `JSON.parse` 炸掉 */
  argsText?: string;
}

export interface ContentBlock {
  type: string;
  [key: string]: unknown;
}

export interface ChatMessage {
  id: string;
  type: MessageType;
  content: string | ContentBlock[];
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  name?: string;
  status?: "success" | "error";

  // ---- 服务端拥有的展示元数据 ----
  /**
   * thread 内全局递增的展示序号（M4 §4.3 的 `run_events.seq`）。
   * **绝不写回 checkpoint**——它是展示用的，不是状态的一部分（M5 §4.2）。
   */
  seq?: number | null;
  run_id?: string | null;
  created_at?: string | null;
  duration_ms?: number | null;

  /** 隐藏的控制副本：只在没有可见副本携带位置时才贡献位置（§4.2） */
  hidden?: boolean;
  /** 用户刚发出、服务端还没确认的那条（§4.4：永远在最后） */
  optimistic?: boolean;

  additional_kwargs?: Record<string, unknown>;
  response_metadata?: Record<string, unknown>;
}

/** `GET /threads/{id}/state` 返回的 checkpoint 快照里的 ThreadState（M1 §2.1） */
export interface ThreadState {
  messages?: ChatMessage[];
  sandbox?: SandboxState | null;
  artifacts?: string[];
  todos?: TodoItem[] | null;
  viewed_images?: Record<string, ViewedImageData>;
  delegations?: DelegationEntry[];
  skill_context?: SkillEntry[];
  promoted?: string[];
  thread_data?: ThreadDataState | null;
  title?: string | null;
  uploaded_files?: UploadedFile[] | null;
  summary_text?: string | null;
}

export interface SandboxState {
  sandbox_id: string;
  [key: string]: unknown;
}

export interface TodoItem {
  content: string;
  status?: "pending" | "in_progress" | "completed";
}

export interface ViewedImageData {
  [key: string]: unknown;
}

export interface DelegationEntry {
  /** 关联键：task 工具的 tool_call_id */
  id: string;
  description?: string;
  subagent_type?: string;
  /** 内部状态 in_progress 或终态 completed/failed/cancelled/timed_out/polling_timed_out */
  status?: string;
  created_at?: string;
  run_id?: string;
  result_brief?: string;
  result_sha256?: string;
  stop_reason?: string;
  [key: string]: unknown;
}

export interface SkillEntry {
  [key: string]: unknown;
}

export interface ThreadDataState {
  workspace_path?: string;
  uploads_path?: string;
  outputs_path?: string;
  [key: string]: unknown;
}

export interface UploadedFile {
  path?: string;
  name?: string;
  size?: number;
  [key: string]: unknown;
}

/** 侧栏用的 thread 投影（M4 §4.1 的 `threads_meta`） */
export interface ThreadSummary {
  thread_id: string;
  display_name?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  metadata?: Record<string, unknown>;
}

export interface RunSummary {
  run_id: string;
  thread_id: string;
  assistant_id?: string | null;
  status: "pending" | "running" | "success" | "error" | "timeout" | "interrupted";
  created_at?: string | null;
  updated_at?: string | null;
  metadata?: Record<string, unknown>;
}

/**
 * `event: gap` 帧（M3 §7.3 / §8.1）。
 * 三个 id 都要，客户端要拿 `latest_available_event_id` 做续订点。
 */
export interface StreamGapFrame {
  code: "stream_replay_gap";
  run_id?: string;
  requested_event_id: string | null;
  earliest_available_event_id: string | null;
  latest_available_event_id: string | null;
  recovery: "reload_durable_state";
}

export const ARTIFACT_PREVIEW_MAX_BYTES = 1024 * 1024;
