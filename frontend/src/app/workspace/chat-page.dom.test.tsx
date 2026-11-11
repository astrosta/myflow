import { cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ARTIFACT_PANEL_STORAGE_KEY } from "@/components/workspace/artifact-panel";
import type { MessageGroup } from "@/core/messages/group";
import type { ChatController } from "@/core/threads/chat-controller";

import { ChatPage } from "./chat-page";

vi.mock("@/core/api/artifacts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/artifacts")>();
  return {
    ...actual,
    fetchArtifactPreview: vi.fn(async (_threadId: string, path: string) => ({
      path,
      text: "内容",
      truncated: false,
      totalBytes: 4,
    })),
  };
});

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});

beforeEach(() => {
  window.sessionStorage.clear();
});

const humanGroup: MessageGroup = {
  kind: "human",
  id: "human-u1",
  message: { id: "u1", type: "human", content: "你好" },
};

function makeController(over: Partial<ChatController> = {}): ChatController {
  return {
    threadId: "t1",
    groups: [humanGroup],
    messages: [],
    state: {},
    artifacts: ["flow-field-particles.html"],
    todos: [],
    streaming: false,
    threadLoading: false,
    loadingOlder: false,
    hasMoreBefore: false,
    error: null,
    interrupt: null,
    interruptBusy: false,
    respondToInterrupt: async () => {},
    recovery: null,
    dismissRecovery: () => {},
    submit: async () => {},
    stop: async () => {},
    loadOlder: async () => {},
    ...over,
  };
}

describe("ChatPage 产物下载", () => {
  it("用当前 thread 构造 download=true 的下载链接", async () => {
    window.sessionStorage.setItem(
      ARTIFACT_PANEL_STORAGE_KEY,
      JSON.stringify({ open: true, path: "flow-field-particles.html" }),
    );

    const view = render(<ChatPage controller={makeController()} />);

    const link = await view.findByRole("link", { name: "下载 flow-field-particles.html" });
    expect(link.getAttribute("href")).toBe(
      "/api/threads/t1/artifacts/flow-field-particles.html?download=true",
    );
    expect(link.hasAttribute("download")).toBe(true);
  });
});
