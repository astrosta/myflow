"use client";

import { useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

/**
 * 会话外壳的共享状态：**当前在看哪个会话**，以及"开一个新会话"。
 *
 * ## 为什么需要它
 *
 * 新会话第一次发送时，thread 是 SDK 在 submit 里建出来的，建完要把地址换成
 * /workspace/chats/{id}。这件事**不能走 router.replace** —— 它跨路由段
 * （/workspace 和 /workspace/chats/[id] 是两个 page），会让 ChatRoute 整棵子树
 * 重新挂载，于是：
 *
 * 1. 乐观消息和身份基线随旧实例一起消失；
 * 2. 正在跑的那条 SSE 订阅断在新实例之外。SDK 的 reconnectOnMount 本来能接回来，
 *    但它读的是 **mount 那一刻**的快照（reconnectKey 是个 useMemo，依赖里只有
 *    stream.isLoading / threadId，storage 之后再写也不会让它重算），而 run id
 *    要等 runs/stream 建立之后才写进 sessionStorage —— 所以 join 永远不会发生。
 *
 * 实测：界面退回欢迎页，约 2.6 秒后才靠旧实例 onFinish 失效 ["messages"]、
 * 重拉账本恢复。
 *
 * 所以改地址改用 history.replaceState：**它只动地址栏，不动 React 树**，
 * 首轮因此完全不重挂载，SSE 订阅一直连着。
 *
 * ## 代价，以及这里为什么有 epoch
 *
 * replaceState 之后 Next 自己不知道地址变了（内部路由树还停在 /workspace），
 * 于是有两件事要补：
 *
 * - 侧栏高亮不能只看 usePathname()，要从这里取 activeThreadId；
 * - "新对话"的 router.push("/workspace") 会被 Next 判定成"已经在这儿"而空转，
 *   所以用 epoch 显式重挂载 ChatSession —— 这也是"已经在 /workspace 上再点一次
 *   新对话"唯一的重置手段。
 */
interface ChatSessionValue {
  /** 当前在看的会话。null = 还没落地的新会话 */
  activeThreadId: string | null;
  /** 换会话的世代号。它是 ChatSession 的 key，变了才重挂载 */
  epoch: number;
  /**
   * "点侧栏会话标题，把消息列表拉回最底下"的请求号。
   *
   * 换会话时列表会因 key 变化重挂载，本来就会落到底部；但用户点的若是
   * **当前会话**，thread id 不变、列表不重挂载。递增这个数就是那种情况下
   * 唯一能通知到消息列表的信号。
   */
  scrollToBottomRequest: number;
  /** 路由段带来的会话（进入既有会话时由 ChatRoute 上报） */
  setActiveThreadId: (threadId: string | null) => void;
  /** 会话刚建好：更新高亮 + 只把地址栏换掉，不触发导航 */
  reportThreadCreated: (threadId: string) => void;
  /** 开一个新会话 */
  newChat: () => void;
  /** 用户点了某个会话标题：让消息列表滚到最新一条 */
  requestScrollToBottom: () => void;
}

const ChatSessionContext = createContext<ChatSessionValue | null>(null);

export function ChatSessionProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null);
  const [epoch, setEpoch] = useState(0);
  const [scrollToBottomRequest, setScrollToBottomRequest] = useState(0);

  const reportThreadCreated = useCallback((threadId: string) => {
    setActiveThreadId(threadId);
    /**
     * 只动地址栏。
     *
     * 不要换成 router.replace —— 那会重挂载 ChatRoute，正是这个模块存在的原因。
     * 地址仍然是对的：刷新页面时 Next 直接按 /workspace/chats/{id} 加载，
     * 历史从账本里来。
     */
    window.history.replaceState(null, "", "/workspace/chats/" + threadId);
  }, []);

  const requestScrollToBottom = useCallback(() => {
    setScrollToBottomRequest((n) => n + 1);
  }, []);

  const newChat = useCallback(() => {
    setActiveThreadId(null);
    setEpoch((n) => n + 1);
    /**
     * 地址还在 /workspace 时这次 push 是空转（Next 认为已经在目标路由上），
     * 真正起作用的是上面的 epoch。地址在 /workspace/chats/{id} 时它才是导航。
     */
    router.push("/workspace");
  }, [router]);

  const value = useMemo(
    () => ({
      activeThreadId,
      epoch,
      newChat,
      reportThreadCreated,
      requestScrollToBottom,
      scrollToBottomRequest,
      setActiveThreadId,
    }),
    [activeThreadId, epoch, newChat, reportThreadCreated, requestScrollToBottom, scrollToBottomRequest],
  );

  return <ChatSessionContext.Provider value={value}>{children}</ChatSessionContext.Provider>;
}

export function useChatSession(): ChatSessionValue {
  const value = useContext(ChatSessionContext);
  if (value === null) throw new Error("useChatSession 必须放在 ChatSessionProvider 里");
  return value;
}
