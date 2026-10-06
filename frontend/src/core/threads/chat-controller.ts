import type { StreamRecoveryEvent } from "../api/gap";
import type { MessageGroup } from "../messages/group";
import type { ChatMessage, ThreadState, TodoItem } from "../types";

/**
 * 聊天页唯一的状态入口。组件层**只**消费这个接口，不认识 SDK。
 *
 * 这样划分的理由是 M5 §2：顺序合并、状态折叠、消息分组都是纯函数，
 * 放 core/ 里能用 node 环境的单测覆盖；一旦混进组件就只能靠渲染测试，
 * 慢 3 倍而且难断言。组件层因此不该有机会碰到 `useStream` 的返回值。
 */
export interface ChatController {
  /**
   * `null` = 这个会话还没有 thread（`/workspace` 的新会话入口）。
   * SDK 会在第一次 submit 时创建，创建完通过 `onThreadCreated` 通知上层。
   */
  threadId: string | null;

  /** `mergeMessages` + `buildMessageGroups` 之后、可直接渲染的分组 */
  groups: MessageGroup[];
  /** 排序合并后的扁平列表。工具结果查找、消息计数这类用途 */
  messages: ChatMessage[];
  /** 折叠后的 ThreadState。`artifacts` 是权威的 artifact 清单（§6.5） */
  state: ThreadState;
  artifacts: string[];
  todos: TodoItem[];

  /** 这一轮正在流。决定纯内容 AI 消息留在"处理中"还是外提成气泡（§6.1） */
  streaming: boolean;
  /** 还在拉第一页历史 */
  threadLoading: boolean;
  /** 加载更旧的一页 */
  loadingOlder: boolean;
  hasMoreBefore: boolean;

  /** 致命错误（流断了、run 失败了）。`null` 表示没有 */
  error: string | null;
  /**
   * gap 恢复提示。**非空时界面必须显示"正在恢复"，不能显示"已完成"**——
   * 后端的 run 还在跑（§5.2）。
   */
  recovery: StreamRecoveryEvent | null;
  dismissRecovery: () => void;

  /** 发一条消息。内部会先插乐观消息（§4.4：乐观消息永远在最后） */
  submit: (text: string, files?: readonly File[]) => Promise<void>;
  /** 停止当前 run。会**立刻失效相关缓存并安排一次跟进刷新**（§5.1） */
  stop: () => Promise<void>;
  /** 翻页加载更旧的历史（§4.4：失效时保留已加载的页） */
  loadOlder: () => Promise<void>;
}
