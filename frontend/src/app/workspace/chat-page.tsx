"use client";

import { useCallback } from "react";

import type { ArtifactPreviewLoader } from "@/components/workspace/artifact-panel";
import type { ModelControlProps } from "@/components/workspace/composer";
import { ChatView } from "@/components/workspace/chat-view";
import { artifactContentUrl, fetchArtifactPreview } from "@/core/api/artifacts";
import type { ChatController } from "@/core/threads/chat-controller";

export interface ChatPageProps {
  controller: ChatController;
  /** 模型 / 推理等级选择条（目录来自 `/api/models`，由路由层取数） */
  modelControl?: ModelControlProps;
  /** 侧栏点击会话标题的次数；变化时消息列表滚到最新一条 */
  scrollToBottomRequest?: number;
}

/**
 * 实际的聊天组件。**唯一的状态入口是 props 里的 controller**：
 * 这里不认识 SDK、不认识 useStream（M5 §2）。
 *
 * artifact 预览的取数函数在这一层构造（core 的 fetchArtifactPreview 只被
 * 应用层引用），下面的 components/workspace 因此保持"只接 props"的形状。
 */
export function ChatPage({ controller, modelControl, scrollToBottomRequest }: ChatPageProps) {
  const threadId = controller.threadId;

  const loadArtifactPreview = useCallback<ArtifactPreviewLoader>(
    (path, options) => {
      // 还没有 thread 就没有产物清单，面板也不会请求。这里只是让类型收窄；
      // 真要走到说明状态不一致，宁可报错也不要静默返回空内容。
      if (threadId === null) {
        return Promise.reject(new Error("这个会话还没有 thread，读不到产物"));
      }
      return fetchArtifactPreview(threadId, path, options ?? {});
    },
    [threadId],
  );

  const artifactDownloadUrl = useCallback(
    (path: string) => {
      if (threadId === null) {
        // 下载链接只在有 thread 时注入；走到这里说明状态不一致。
        throw new Error("这个会话还没有 thread，下载不了产物");
      }
      return artifactContentUrl(threadId, path, true);
    },
    [threadId],
  );

  return (
    <ChatView
      artifactDownloadUrl={threadId === null ? undefined : artifactDownloadUrl}
      controller={controller}
      loadArtifactPreview={loadArtifactPreview}
      modelControl={modelControl}
      scrollToBottomRequest={scrollToBottomRequest}
    />
  );
}
