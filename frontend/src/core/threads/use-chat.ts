"use client";

import { useStream } from "@langchain/langgraph-sdk/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { toChatMessages } from "../api/adapt";
import { getApiClient } from "../api/client";
import { WILDCARD_THREAD, registerRecoveryContext } from "../api/fetch-with-gap";
import type { StreamRecoveryEvent } from "../api/gap";
import { DEFAULT_PAGE_SIZE, fetchMessagePage } from "../api/messages-feed";
import { sanitizeChatStreamModes } from "../api/stream-mode";
import { getThreadState } from "../api/threads";
import { uploadFiles } from "../api/uploads";
import { buildMessageGroups } from "../messages/group";
import {
  createHistoryWindow,
  hasMoreBefore,
  loadedFloorSeq,
  loadedMessages,
  prependOlderPage,
  replaceNewestPage,
  type MessagePage,
} from "../messages/history";
import { messageKey } from "../messages/identity";
import { mergeMessages, type TurnBaseline } from "../messages/order";
import type { RunContext } from "../models/selection";
import type { ChatMessage, ThreadState } from "../types";
import type { ChatController } from "./chat-controller";
import { buildResumeCommand, parseChatInterrupt } from "./interrupts";
import { foldUpdateFrame, isRelevantUpdateFrame } from "./stream-state";
import {
  createOptimisticHumanMessage,
  createTurnBaseline,
  reconcileOptimistic,
} from "./turn-baseline";

export const DEFAULT_ASSISTANT_ID = "lead_agent";

/** 没有模型选择时的空装配上下文（常量引用，避免每次渲染都新建对象） */
const EMPTY_RUN_CONTEXT: RunContext = {};

/**
 * 停止之后安排一次跟进刷新的延迟（§5.1）。
 *
 * SDK 的停止可能通过 "abort + 发射后不管的 cancel" 完成，在后端的标题生成
 * 落库之前就返回了。只失效一次的话，侧栏里会留着"新对话"这个占位标题。
 */
export const STOP_FOLLOW_UP_REFRESH_MS = 1_500;

const messagesQueryKey = (threadId: string | null) => ["messages", threadId] as const;

export interface UseChatOptions {
  /**
   * `null` = 这个会话还没有 thread（`/workspace` 的新会话入口）。
   * SDK 会在第一次 submit 时自己建一个，然后回调 `onThreadCreated`。
   *
   * 这么做而不是"进页面就建一个空 thread"，是因为后者会往侧栏塞一堆
   * 没人用过的空会话——用户只是路过工作区，不该留下垃圾。
   */
  threadId: string | null;
  assistantId?: string;
  /** thread 建好之后的回调：用来把 URL 换成 `/workspace/chats/{id}` */
  onThreadCreated?: (threadId: string) => void;
  /**
   * 每一轮都要带上的装配上下文（`model_name` / `reasoning_effort`）。
   *
   * 走 `runs.create` 的 `context`，网关的 collect_client_switches 认的是这几个键，
   * 并会按 config.yaml 的模型白名单与推理等级再校验一次。空对象就是"不选"，
   * 服务端用 agents.default_model 与 provider 默认等级。
   */
  runContext?: RunContext;
}

/**
 * 聊天页的状态装配点。
 *
 * 这里刻意只做"接线"：所有顺序、折叠、分组的判断都在 core/ 的纯函数里，
 * 这个 hook 的职责是把 SDK 的流、分页的历史和乐观消息喂给它们。
 */
export function useChat({
  threadId,
  assistantId = DEFAULT_ASSISTANT_ID,
  onThreadCreated,
  runContext,
}: UseChatOptions): ChatController {
  const queryClient = useQueryClient();

  /**
   * 这个会话真正在用的 thread id。
   *
   * `threadId` 传 null 表示"还没落地的新会话"，SDK 会在第一次 submit 时自己建一个。
   * 建完之后 **必须把它记下来**：不然这个 hook 里所有以 threadId 为键的东西
   * （历史 query、gap 恢复注册、附件上传）会一直以为"还没有 thread"——
   * 历史永远不加载，账本里那条 `llm.human.input` 也就永远回不来，
   * 用户消息会卡在"发送中…"一辈子。
   *
   * 注意这条路径**不能靠重新挂载解决**：URL 是用 `history.replaceState` 换的
   * （见 chat-route），组件不会重建，所以只能在这里接住。
   */
  const [createdThreadId, setCreatedThreadId] = useState<string | null>(null);

  /**
   * 地址只落地一次。`onCreated` 每起一个 run 都会回调，而"把新会话的地址写进 URL"
   * 只在第一个 run 需要——后续 run 的地址已经是它了。
   */
  const navigatedRef = useRef(false);

  const effectiveThreadId = threadId ?? createdThreadId;
  const hasThread = effectiveThreadId !== null;
  const client = useMemo(() => getApiClient(), []);

  /**
   * §3.1：主聊天流**强制**成增量模式。请求层还会再覆盖一次 body（见 client.ts）。
   *
   * `messages-tuple` 显式写出来：token 级的正文/思考增量只从它来（`values` 只在节点
   * 结束时给整份状态，等于没有流式）。`sanitizeChatStreamModes` 保证增量模式集永远
   * 含它，这里写出来是让"我们确实订阅了 messages-tuple"在代码里可读。
   */
  const streamModes = useMemo(() => sanitizeChatStreamModes(["messages-tuple"]), []);

  /** 提交时要带的装配上下文。上层已用 useMemo 固定引用，这里只补个空对象兜底 */
  const runContextValue = runContext ?? EMPTY_RUN_CONTEXT;

  const [recovery, setRecovery] = useState<StreamRecoveryEvent | null>(null);
  const [optimistic, setOptimistic] = useState<ChatMessage[]>([]);
  const [turnBaseline, setTurnBaseline] = useState<TurnBaseline | null>(null);
  const [olderPages, setOlderPages] = useState<MessagePage[]>([]);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [interruptBusy, setInterruptBusy] = useState(false);

  // ---- 持久化历史 ----
  const newestPage = useQuery({
    queryKey: [...messagesQueryKey(effectiveThreadId), "newest"],
    queryFn: ({ signal }) => {
      // enabled 已经挡住了 null 的情况，这里只是让类型收窄
      if (effectiveThreadId === null) throw new Error("no thread yet");
      return fetchMessagePage(effectiveThreadId, { limit: DEFAULT_PAGE_SIZE, signal });
    },
    // 还没有 thread 就没有历史可拉；第一次 submit 建出 thread 后会重新取
    enabled: hasThread,
  });

  /**
   * §4.4：**历史缓存失效时保留已加载的页。**
   *
   * 结构上就保证了这一点：`olderPages` 是本地状态，重取第一页只换 `pages[0]`，
   * 碰不到更旧的页。已经建立的排序位置因此不会被丢掉，列表不会重新洗牌。
   */
  const historyWindow = useMemo(() => {
    let acc = createHistoryWindow();
    if (newestPage.data) acc = replaceNewestPage(acc, newestPage.data);
    for (const page of olderPages) acc = prependOlderPage(acc, page);
    return acc;
  }, [newestPage.data, olderPages]);

  const history = useMemo(() => loadedMessages(historyWindow), [historyWindow]);
  const floorSeq = useMemo(() => loadedFloorSeq(historyWindow), [historyWindow]);
  const moreBefore = useMemo(() => hasMoreBefore(historyWindow), [historyWindow]);

  // ---- 实时流 ----
  const stream = useStream<ThreadState>({
    assistantId,
    /**
     * 这里必须传 `effectiveThreadId`，不能传 `threadId` 这个 prop。
     *
     * `useStream` 的 `threadId` 是**受控**的：只要 options 里带了这个键，
     * SDK 就永远用它的值，不会用自己 `onThreadId` 之后记住的本地值。而新会话在
     * 首轮结束前 prop 一直是 null（地址是 replaceState 换的，路由树不重挂载），
     * 于是第二轮 submit 会**再建一个 thread**：用户以为还在同一个会话里，实际
     * 消息跑到了新 thread 上。后端按 thread 派生 sandbox_id，新 thread 的
     * sandbox 与前端折叠到的旧 sandbox 不同 → `Reducer conflict on "sandbox"`。
     */
    threadId: effectiveThreadId,
    client,
    /**
     * §3.3：用 SDK 自带的节流——"同一个宏任务内到达的更新先合并再通知 React"。
     * 不要换成固定毫秒数的延迟，那在连续流上会变成尾部防抖，
     * 表现为"模型说完了但界面还在挤牙膏"。
     */
    throttle: true,
    /** §5.1：刷新时 join 正在跑的 run，而不是重新发一次消息 */
    reconnectOnMount: true,
    /**
     * SDK 在第一次 submit 时把 thread 建出来，id 从这里回来。
     * 侧栏要立刻刷新，否则新会话不会出现在列表里。
     */
    onThreadId: (newThreadId) => {
      setCreatedThreadId(newThreadId);
      queryClient.invalidateQueries({ queryKey: ["threads"] });
      // 这里**不发导航**，原因见下面 onCreated 的注释
    },
    /**
     * §5.1：**改地址必须等到 run 真正建立**，不能放在 `onThreadId`。
     *
     * 新会话第一次发送时 `/workspace` → `/workspace/chats/{id}` 是跨路由段的
     * 导航，ChatRoute 整棵子树会重新挂载。重挂载本身没问题——SDK 的
     * `reconnectOnMount` 就是为它设计的：新实例从 sessionStorage 读
     * `lg:stream:{threadId}` 拿到 run_id，再 `joinStream` 把流接回来。
     *
     * 问题出在**读的时机**。`reconnectKey` 是个 `useMemo`，依赖只有
     * `runMetadataStorage` / `stream.isLoading` / `threadId`——它在 mount 时求值
     * 一次，之后 storage 再变化**不会让它重算**。而那个键是在 `runs/stream`
     * 建立之后才写进去的（SDK 内部先写 storage、再回调 `onCreated`）。
     *
     * 所以只要在 `onThreadId` 那一刻导航（它由 `threads.create()` 返回触发，
     * 早于 run 的建立），新实例 mount 时 storage 还是空的 → `reconnectKey`
     * 求值成 undefined → **join 永远不会发生**。表现就是：乐观消息随旧实例
     * 一起没了，历史页又是空的，界面退回欢迎页；要等旧实例（虽然已卸载，
     * 但 fetch 没被 abort）的 `onFinish` 失效 `["messages"]`、把账本重拉回来
     * 才恢复——实测约 2.6 秒的空窗。
     *
     * 放到 `onCreated` 就对齐了：这个回调点就在 storage 写入之后。
     */
    onCreated: (run) => {
      // 已存在的会话不需要改地址（threadId 就是当前路由段）；新会话只写一次
      if (threadId !== null || navigatedRef.current) return;
      navigatedRef.current = true;
      onThreadCreated?.(run.thread_id);
    },
    onUpdateEvent: (data, { mutate }) => {
      // §3.2：无关的帧要在调用 SDK 的状态改写器**之前**就拒掉
      if (!isRelevantUpdateFrame(data)) return;
      mutate((prev) => foldUpdateFrame(prev as ThreadState, data) as never);
    },
    onFinish: () => {
      setTurnBaseline(null);
      /**
       * 用**前缀** key 失效，不按具体 threadId。
       *
       * SDK 在 mount 时就把 options 固定住了，所以这个闭包拿到的是**当时的**
       * effectiveThreadId —— 新会话首轮时它还是 null，"if (hasThread)" 会直接
       * 跳过失效，于是 run 结束后账本永远不重拉，用户消息卡在"发送中…"。
       * `["messages"]` 是所有消息 query 的公共前缀，绕开这个过期闭包。
       */
      queryClient.invalidateQueries({ queryKey: ["messages"] });
      /**
       * 标题也是这一轮收尾时才落库的：worker 在 finally 里把 checkpoint 的
       * `title` 投影到 `threads_meta.display_name`（M4 §4.1），然后才发 end 帧。
       * 侧栏读的是 ["threads"] 列表缓存，不失效的话它会继续显示"新对话"，
       * 直到用户刷新页面。
       */
      queryClient.invalidateQueries({ queryKey: ["threads"] });
    },
  });

  const streamMessages = useMemo(
    () => toChatMessages(stream.messages as unknown[] | undefined),
    [stream.messages],
  );

  /**
   * SDK 会从 updates 帧里的 __interrupt__ 维护中断列表；这里只把它转成
   * 面板需要的视图模型。刷新页面时 SDK 也会从 thread tasks 恢复同一份列表。
   */
  const interrupt = useMemo(
    () => parseChatInterrupt(stream.interrupts),
    [stream.interrupts],
  );

  /**
   * §5.2：把恢复语义注册给 gap 包装层。
   *
   * 依赖全是稳定引用（`client`/`queryClient`/`threadId`），所以这个 effect
   * 只在换会话时重跑一次——注册表在 gap 发生的那一刻必须已经有值，
   * 反复注册/注销会留下一个空窗期。
   */
  useEffect(() => {
    /**
     * 还没有 thread 时注册到**通配键**上：第一次 submit 会就地建出 thread，
     * 从"请求发出"到"thread 建好"这段窗口里的 gap 也必须能恢复——
     * 否则它会表现为一次正常的流结束（§5.2 明确禁止的那种）。
     * 回调收到的 thread_id 是从请求 URL 里解析出来的，不依赖注册时的值。
     */
    return registerRecoveryContext(effectiveThreadId ?? WILDCARD_THREAD, {
      reloadDurableState: async (resolvedThreadId) => {
        // §5.2 第 3 条：用 `values` 重载持久化的线程状态（§3.1 那个例外）
        // 不用 SDK 的 getState：网关返回 { values, checkpoint_id, checkpoint_ns }，
        // 和 Platform 那套 { values, next, checkpoint, metadata, tasks } 对不上。
        await getThreadState(resolvedThreadId);
        await queryClient.refetchQueries({ queryKey: messagesQueryKey(resolvedThreadId) });
      },
      onRecovery: (event) => {
        setRecovery(event);
        // 上层要清掉乐观状态、失效历史缓存、给用户看一个恢复提示
        setOptimistic([]);
        setTurnBaseline(null);
        // 同样用前缀：恢复回调也可能被旧闭包调用
        queryClient.invalidateQueries({ queryKey: ["messages"] });
      },
    });
  }, [effectiveThreadId, client, queryClient]);

  /**
   * 乐观消息的处置（三种状态都写进 `reconcileOptimistic`）：
   *
   * **不能只按 id 判。** 服务端可能把我们提交的那条换成新 id（`local-xxx` →
   * `human-1` 或一个 uuid + `__user`），这时按 id 既认不出"已被确认"，也去不掉重复。
   * 所以：
   *
   * - 服务端已经把同样正文的提问发回来 → 去掉乐观副本（否则提问显示两遍）；
   * - 已经开始出步骤、但提问还没回来 → 保留这条，只摘掉"发送中…"（问题不能消失）；
   * - 什么反应都还没有 → 原样保留，"发送中…"继续显示。
   */
  const optimisticState = useMemo(
    () => reconcileOptimistic(optimistic, streamMessages, turnBaseline),
    [optimistic, streamMessages, turnBaseline],
  );

  // ---- 合并 + 分组 ----
  const messages = useMemo(
    () =>
      mergeMessages({
        history,
        stream: streamMessages,
        optimistic: optimisticState.messages,
        loadedFloorSeq: floorSeq,
        turnBaseline,
      }),
    [history, streamMessages, optimisticState, floorSeq, turnBaseline],
  );

  const groups = useMemo(
    () =>
      buildMessageGroups({
        messages,
        // 流式期间纯内容 AI 消息留在"处理中"分组（§6.1）
        streaming: Boolean(stream.isLoading),
      }),
    [messages, stream.isLoading],
  );

  const loadOlder = useCallback(async () => {
    // 还没有 thread 就没有更早的页可翻
    if (effectiveThreadId === null) return;
    const cursor = historyWindow.pages.at(-1)?.nextBeforeSeq ?? null;
    if (cursor === null || loadingOlder) return;
    setLoadingOlder(true);
    try {
      const page = await fetchMessagePage(effectiveThreadId, { beforeSeq: cursor });
      setOlderPages((prev) => [...prev, page]);
    } finally {
      setLoadingOlder(false);
    }
  }, [effectiveThreadId, historyWindow, loadingOlder]);

  const submit = useCallback(
    async (text: string, files?: readonly File[]) => {
      if (!text.trim()) return;

      // 先传文件再起 run：M6 的 UploadsMiddleware 在 `before_agent` 里
      // 读取本轮 human message 的 additional_kwargs.files 并注入清单。
      // 这里必须把上传结果随消息一起提交；只传文本会让模型看不到附件。
      //
      // threadId 为 null 时不允许带附件（UI 也把附件按钮禁掉了）：上传需要一个
      // 已存在的 thread，而这里若是先建 thread 再 submit，SDK 会**再建一个**，
      // 于是文件传到了 A、对话跑在 B。
      let uploadedFiles: Awaited<ReturnType<typeof uploadFiles>> = [];
      if (files?.length) {
        if (effectiveThreadId === null) throw new Error("请先发送一条消息，再上传附件");
        uploadedFiles = await uploadFiles(effectiveThreadId, files);
        if (uploadedFiles.length !== files.length) {
          throw new Error("上传接口没有返回完整的文件清单，请重试");
        }
      }

      const optimisticMessage = createOptimisticHumanMessage({ text });
      /**
       * §4.4：本地提交的那一轮要记一个身份基线。
       * 基线取的是**提交这一刻**屏幕上的所有身份，之后才分得清
       * "流新发布的步骤"和"本来就在那儿的步骤"。
       */
      setTurnBaseline(createTurnBaseline(messages, messageKey(optimisticMessage), null));
      setOptimistic((prev) => [...prev, optimisticMessage]);

      try {
        await stream.submit(
          // 身份由客户端定：M6 的动态上下文中间件会把它改名为 X__user，
          // UI 侧的身份归一化让两条认成同一个可见轮次（§4.4）
          {
            messages: [
              {
                type: "human",
                content: text,
                id: optimisticMessage.id,
                ...(uploadedFiles.length
                  ? {
                      additional_kwargs: {
                        files: uploadedFiles.map((file) => ({
                          filename: file.filename,
                          size: file.size ?? 0,
                        })),
                      },
                    }
                  : {}),
              },
            ],
          } as never,
          {
            streamMode: streamModes as never,
            /** model_name / reasoning_effort：空对象时网关按默认模型处理 */
            context: runContextValue as never,
            /**
             * 显式写出来，哪怕它和默认值一样。
             *
             * **断开连接就该取消这一轮**（M3 §8.2 的创建端语义）：用户关掉页面
             * 的意图就是"别跑了"，让它继续烧 token 是错的。要做"刷新续流"靠的是
             * §5.1 的 join——重新连上去订阅同一个 run，而不是让 run 在你不在的时候
             * 接着跑。
             */
            onDisconnect: "cancel",
          },
        );
      } catch (error) {
        setOptimistic((prev) => prev.filter((item) => item.id !== optimisticMessage.id));
        setTurnBaseline(null);
        throw error;
      }
    },
    [stream, streamModes, effectiveThreadId, messages, runContextValue],
  );

  const respondToInterrupt = useCallback(
    async (decision: "approve" | "reject") => {
      if (!interrupt || interruptBusy) return;
      setInterruptBusy(true);
      try {
        /**
         * 恢复不是"再发一条普通消息"：input 必须为空，Command(resume=...)
         * 让 LangGraph 从 checkpoint 里的 interrupt 继续，而不是重跑这一轮。
         */
        await stream.submit(null as never, {
          command: buildResumeCommand(interrupt, decision) as never,
          context: runContextValue as never,
          onDisconnect: "cancel",
          streamMode: streamModes as never,
        });
      } finally {
        setInterruptBusy(false);
      }
    },
    [interrupt, interruptBusy, runContextValue, stream, streamModes],
  );

  const stop = useCallback(async () => {
    await stream.stop();
    // §5.1：立刻失效相关缓存，**并安排一次跟进刷新**
    queryClient.invalidateQueries({ queryKey: messagesQueryKey(effectiveThreadId) });
    queryClient.invalidateQueries({ queryKey: ["threads"] });
    globalThis.setTimeout(() => {
      queryClient.invalidateQueries({ queryKey: ["threads"] });
    }, STOP_FOLLOW_UP_REFRESH_MS);
  }, [stream, queryClient, effectiveThreadId]);

  const dismissRecovery = useCallback(() => setRecovery(null), []);

  const state = useMemo(() => {
    const values = (stream.values ?? {}) as ThreadState;
    return {
      ...values,
      artifacts: values.artifacts ?? [],
      todos: values.todos ?? null,
    } satisfies ThreadState;
  }, [stream.values]);

  return {
    threadId: effectiveThreadId,
    groups,
    messages,
    state,
    artifacts: state.artifacts ?? [],
    todos: state.todos ?? [],
    streaming: Boolean(stream.isLoading),
    threadLoading: Boolean(stream.isThreadLoading) || newestPage.isLoading,
    loadingOlder,
    hasMoreBefore: moreBefore,
    error: stream.error ? String((stream.error as Error).message ?? stream.error) : null,
    interrupt,
    interruptBusy,
    respondToInterrupt,
    recovery,
    dismissRecovery,
    submit,
    stop,
    loadOlder,
  };
}
