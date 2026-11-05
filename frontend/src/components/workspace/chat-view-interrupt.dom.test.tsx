import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { ChatController } from "@/core/threads/chat-controller";

import { ChatView } from "./chat-view";

afterEach(cleanup);

function controller(interrupt: ChatController["interrupt"]): ChatController {
  return {
    threadId: "t1",
    groups: [],
    messages: [],
    state: {},
    artifacts: [],
    todos: [],
    streaming: false,
    threadLoading: false,
    loadingOlder: false,
    hasMoreBefore: false,
    error: null,
    interrupt,
    interruptBusy: false,
    respondToInterrupt: async () => {},
    recovery: null,
    dismissRecovery: () => {},
    submit: async () => {},
    stop: async () => {},
    loadOlder: async () => {},
  };
}

function renderView(value: ChatController) {
  return render(
    <ChatView
      controller={value}
      loadArtifactPreview={async (path) => ({ path, text: "", truncated: false, totalBytes: 0 })}
    />,
  );
}

describe("ChatView 的图级中断", () => {
  it("有待确认项时显示确认面板并锁住输入框", () => {
    const view = renderView(
      controller({
        id: "i1",
        toolName: "update_user_profile",
        description: "确认更新画像",
        allowedDecisions: ["approve", "reject"],
        actionCount: 1,
      }),
    );

    expect(view.getByRole("alertdialog", { name: "需要你确认" })).toBeTruthy();
    expect(view.getByLabelText("消息输入框")).toHaveProperty("disabled", true);
  });
});
