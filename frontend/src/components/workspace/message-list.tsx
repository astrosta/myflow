"use client";

import { useCallback, useEffect, useRef } from "react";

import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { pickDurationAnchor, type MessageGroup } from "@/core/messages/group";
import type { ChatMessage } from "@/core/types";
import { cn, formatDuration } from "@/lib/utils";

import { AnswerBubble } from "./answer-bubble";
import { HumanBubble } from "./human-bubble";
import { StepsPanel } from "./steps-panel";

/** 距底部多少像素以内算"用户还在跟着看" */
const PIN_THRESHOLD_PX = 80;

/**
 * 取一个分组的锚点消息。
 *
 * §6.3：运行时长这类按 run 解析的展示**只挂在一个锚点上**，否则一次 run 里
 * 每条 AI 消息都会渲染一份一模一样的。锚点的选择由 core 的
 * pickDurationAnchor 做（它接受任何分组类型），这里只负责把值取出来。
 */
function anchorMessage(group: MessageGroup): ChatMessage | null {
  if (group.kind === "human") return group.message;
  if (group.kind === "answer") return group.message;
  const matched = group.steps.find((step) => step.message.id === group.anchorMessageId);
  return (matched ?? group.steps.at(-1))?.message ?? null;
}

export interface MessageListProps {
  groups: readonly MessageGroup[];
  streaming?: boolean;
  /** 首屏历史还在加载 */
  loading?: boolean;
  hasMoreBefore?: boolean;
  loadingOlder?: boolean;
  onLoadOlder?: () => void;
  /** 正在做 gap 恢复（§5.2）：步骤面板不能说"已完成" */
  recovering?: boolean;
  /**
   * 用户在侧栏点了一次会话标题。
   *
   * 换会话时 ChatRoute 会重挂载列表，本来就会落到底部；但**点的是当前会话**
   * 时 thread id 没变，列表不会重挂载。这个递增的请求号是那种情况下
   * "把我拉回最新一条"的唯一信号。
   */
  scrollToBottomRequest?: number;
  className?: string;
}

export function MessageList({
  groups,
  streaming = false,
  loading = false,
  hasMoreBefore = false,
  loadingOlder = false,
  onLoadOlder,
  recovering = false,
  scrollToBottomRequest = 0,
  className,
}: MessageListProps) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  const anchorRef = useRef<{ height: number; top: number } | null>(null);

  // 跟随还是别跟随，取决于用户现在在哪：只有本来就在底部的人才应该被
  // 新内容带走。往上翻历史的人被不断拽回底部是最典型的聊天页面 bug。
  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return;
    const onScroll = () => {
      pinnedRef.current =
        element.scrollHeight - element.scrollTop - element.clientHeight <= PIN_THRESHOLD_PX;
    };
    /**
     * **不要在挂载时立刻调一次 onScroll()。**
     *
     * 点回一个已经缓存过的会话时，React Query 的 messages 会在首帧就带着
     * 整页历史进来；此时视口天然在 scrollTop=0，一调 onScroll 就会把它判成
     * "用户正在往上翻"，随后的 groups effect 于是拒绝滚到底部——表现就是
     * 打开一个旧会话停在最上面。用户还没动过滚轮，pinned 必须保持初始的 true。
     */
    element.addEventListener("scroll", onScroll, { passive: true });
    return () => element.removeEventListener("scroll", onScroll);
  }, []);

  // 翻页后把视口停在用户刚才看的那条消息上：新内容插在**上面**，
  // 不做高度补偿的话整屏会往下跳一截。
  useEffect(() => {
    const element = viewportRef.current;
    const anchor = anchorRef.current;
    if (!element || !anchor || loadingOlder) return;
    element.scrollTop = anchor.top + (element.scrollHeight - anchor.height);
    anchorRef.current = null;
  }, [groups, loadingOlder]);

  // 在底部的人才跟着新内容走；往上翻历史的人不动
  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return;
    if (pinnedRef.current) element.scrollTop = element.scrollHeight;
  }, [groups]);

  /**
   * 明确的"回到最新一条"请求：点侧栏会话标题，或用户自己发送一条消息。
   *
   * 放在 groups 的跟随 effect **之后**：这两种动作是用户当下最强的意图，
   * 不应该被同一帧里的高度补偿或跟随判断覆盖。这里同时把 pinned 重置回 true，
   * 让后续到达的流式内容继续跟随。
   */
  const lastScrollRequestRef = useRef(scrollToBottomRequest);
  useEffect(() => {
    if (lastScrollRequestRef.current === scrollToBottomRequest) return;
    lastScrollRequestRef.current = scrollToBottomRequest;
    const element = viewportRef.current;
    if (!element) return;
    pinnedRef.current = true;
    element.scrollTop = element.scrollHeight;
  }, [scrollToBottomRequest]);

  const handleLoadOlder = useCallback(() => {
    const element = viewportRef.current;
    if (element) anchorRef.current = { height: element.scrollHeight, top: element.scrollTop };
    onLoadOlder?.();
  }, [onLoadOlder]);

  const durationAnchorId = pickDurationAnchor(groups);

  return (
    <ScrollArea className={cn("min-h-0 flex-1", className)} ref={viewportRef}>
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-6 px-4 py-6">
        {hasMoreBefore ? (
          <div className="flex justify-center">
            <Button disabled={loadingOlder} onClick={handleLoadOlder} size="sm" variant="secondary">
              {loadingOlder ? "正在加载更早的消息…" : "加载更早的消息"}
            </Button>
          </div>
        ) : null}

        {loading && groups.length === 0 ? (
          <div className="space-y-4" aria-hidden>
            <Skeleton className="ml-auto h-12 w-2/3" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : null}

        {!loading && groups.length === 0 ? (
          <p className="py-16 text-center text-sm text-neutral-400">
            还没有消息。在下面输入框里说点什么，开始这一轮对话。
          </p>
        ) : null}

        {groups.map((group) => {
          // 人类分组没有锚点字段（§6.3 的锚点候选集里本来就没有它）
          const groupAnchorId = group.kind === "human" ? null : group.anchorMessageId;
          const isDurationAnchor = groupAnchorId !== null && groupAnchorId === durationAnchorId;
          const duration = isDurationAnchor ? formatDuration(anchorMessage(group)?.duration_ms) : null;

          return (
            <div className="flex flex-col gap-2" key={group.id}>
              {group.kind === "human" ? <HumanBubble message={group.message} /> : null}

              {group.kind === "steps" ? <StepsPanel group={group} recovering={recovering} /> : null}

              {/* 终态气泡：只有这一轮结束后才会出现，所以不需要流式光标 */}
              {group.kind === "answer" ? <AnswerBubble group={group} /> : null}

              {/* 运行时长：整个列表里只有这一个锚点会渲染它（§6.3） */}
              {duration ? <p className="text-[11px] text-neutral-400">本次运行耗时 {duration}</p> : null}
            </div>
          );
        })}

        {streaming && !groups.some((group) => group.kind === "steps" && group.streaming) ? (
          <p className="flex items-center gap-2 text-xs text-neutral-400">
            <span aria-hidden className="stream-pulse h-1.5 w-1.5 rounded-full bg-emerald-500" />
            正在生成…
          </p>
        ) : null}
      </div>
    </ScrollArea>
  );
}
