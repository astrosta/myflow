"use client";

import type { ChatController } from "@/core/threads/chat-controller";
import { cn } from "@/lib/utils";

import { ArtifactPanel, type ArtifactPreviewLoader } from "./artifact-panel";
import { Composer, type ModelControlProps } from "./composer";
import { MessageList } from "./message-list";
import { StreamStatus } from "./stream-status";
import { TodoPanel } from "./todo-panel";

/**
 * 聊天页的组装层：状态条 + 消息列表 + 输入框 + 产物面板。
 *
 * 唯一的入参是 ChatController（M5 §2 的"组件层只消费这个接口"）：
 * 组件不认识 SDK、不认识 useStream，也不认识网关地址。
 *
 * artifact 预览的取数函数由外层注入，所以这一层（以及它下面的所有组件）
 * 依然是可测的纯展示代码。
 */
export interface ChatViewProps {
  controller: ChatController;
  loadArtifactPreview: ArtifactPreviewLoader;
  /** 模型 / 推理等级选择条；由容器层从 `/api/models` 取数后注入 */
  modelControl?: ModelControlProps;
  className?: string;
}

/**
 * 空会话的引导文案。
 *
 * 只在**确实没有消息、也不在加载**时出现（见 isEmpty 的判定），
 * 所以刷新一个已有会话时不会先闪一下它再跳成消息列表。
 */
function ChatHero() {
  return (
    <div className="w-full max-w-2xl pb-5 text-center">
      <h1 className="text-2xl font-semibold tracking-tight text-neutral-900">
        有什么可以帮你的？
      </h1>
      <p className="mt-2 text-sm leading-relaxed text-neutral-500">
        可以调用工具、读写文件；运行过程和产物会按顺序出现在对话里。
      </p>
    </div>
  );
}

export function ChatView({
  controller,
  loadArtifactPreview,
  modelControl,
  className,
}: ChatViewProps) {
  /**
   * 对话还没开始 —— 输入框要显示在页面中间，而不是贴底。
   *
   * `threadLoading` 必须一起判：拉历史的时候 groups 也是空的，
   * 只看 groups 会让每个已有会话在首屏闪一下 hero。
   */
  const isEmpty = controller.groups.length === 0 && !controller.threadLoading;

  return (
    <div className={cn("flex h-full min-h-0 flex-col bg-white", className)}>
      <StreamStatus
        error={controller.error}
        onDismissRecovery={controller.dismissRecovery}
        recovery={controller.recovery}
        streaming={controller.streaming}
        threadLoading={controller.threadLoading}
      />

      <div className="flex min-h-0 flex-1 flex-col lg:flex-row">
        <div className="flex min-h-0 flex-1 flex-col">
          {/**
           * 空会话：上半段只放引导文案，并且**把它推到这块区域的底部**，
           * 好和紧跟着的输入框贴成一组；配合下面那个等高的空白块，
           * 整组就落在垂直中央。
           *
           * 输入框本身在两种形态下**处在同一个子节点位置**（都是第 3 个），
           * 所以布局切换时 React 不会重建它 —— 草稿、已选附件、错误提示
           * 都不会因为"对话刚开始"而丢掉。
           */}
          {isEmpty ? (
            <div className="flex min-h-0 flex-1 flex-col items-center justify-end px-4">
              <ChatHero />
            </div>
          ) : (
            <MessageList
              groups={controller.groups}
              hasMoreBefore={controller.hasMoreBefore}
              loading={controller.threadLoading}
              loadingOlder={controller.loadingOlder}
              onLoadOlder={() => {
                void controller.loadOlder();
              }}
              recovering={controller.recovery !== null}
              streaming={controller.streaming}
            />
          )}

          {controller.todos.length ? (
            <div className="px-4 pb-2">
              <div className="mx-auto w-full max-w-3xl">
                <TodoPanel todos={controller.todos} />
              </div>
            </div>
          ) : null}

          <Composer
            attachmentsDisabled={controller.threadId === null}
            disabled={controller.threadLoading}
            modelControl={modelControl}
            onSubmit={controller.submit}
            onStop={() => {
              void controller.stop();
            }}
            streaming={controller.streaming}
            variant={isEmpty ? "hero" : "docked"}
          />

          {/* 和上半段等高的留白，把「引导文案 + 输入框」顶到垂直居中 */}
          {isEmpty ? <div aria-hidden className="min-h-0 flex-1" /> : null}
        </div>

        {/** 还没有对话就没有产物面板：在落地态显示"这一轮还没有产物"只是噪声 */}
        {isEmpty ? null : (
          <ArtifactPanel
            artifacts={controller.artifacts}
            className="max-h-[45vh] lg:max-h-none lg:w-80 lg:shrink-0"
            loadPreview={loadArtifactPreview}
            threadLoading={controller.threadLoading}
          />
        )}
      </div>
    </div>
  );
}
