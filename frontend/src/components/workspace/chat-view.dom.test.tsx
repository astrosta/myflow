import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import type { MessageGroup } from "@/core/messages/group";
import type { ChatController } from "@/core/threads/chat-controller";

import { ChatView } from "./chat-view";

/**
 * 空会话把输入框放中间、有对话之后贴底 —— 这条布局判断只在真实 React 下成立
 * （取决于 controller 的几个字段），所以写成 DOM 测试。
 */

afterEach(cleanup);

const HERO_TEXT = "有什么可以帮你的？";

function makeController(over: Partial<ChatController> = {}): ChatController {
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

const humanGroup: MessageGroup = {
  kind: "human",
  id: "human-u1",
  message: { id: "u1", type: "human", content: "你好" },
};

function renderView(controller: ChatController) {
  return render(
    <ChatView
      controller={controller}
      loadArtifactPreview={async (path) => ({ path, text: "", truncated: false, totalBytes: 0 })}
    />,
  );
}

describe("ChatView 的图级中断", () => {
  it("有待确认项时显示确认面板并锁住输入框", () => {
    const view = renderView(
      makeController({
        interrupt: {
          id: "i1",
          toolName: "update_user_profile",
          description: "确认更新画像",
          allowedDecisions: ["approve", "reject"],
          actionCount: 1,
        },
      }),
    );

    expect(view.getByRole("alertdialog", { name: "需要你确认" })).toBeTruthy();
    expect(view.getByLabelText("消息输入框")).toHaveProperty("disabled", true);
  });
});

describe("空会话：输入框在页面中间", () => {
  it("没有消息时显示引导文案", () => {
    const view = renderView(makeController());

    expect(view.getByText(HERO_TEXT)).toBeDefined();
  });

  it("空会话不渲染产物面板（「这一轮还没有产物」在落地态只是噪声）", () => {
    const view = renderView(makeController());

    expect(view.queryByText("这一轮还没有产物。")).toBeNull();
  });

  it("**拉历史时不算空会话** —— 否则每个已有会话首屏都会闪一下 hero", () => {
    const view = renderView(makeController({ threadLoading: true }));

    expect(view.queryByText(HERO_TEXT)).toBeNull();
  });
});

describe("对话开始后：输入框落到底部", () => {
  it("有消息就不再显示引导文案", () => {
    const view = renderView(
      makeController({
        groups: [humanGroup],
        messages: [humanGroup.message],
      }),
    );

    expect(view.queryByText(HERO_TEXT)).toBeNull();
    expect(view.getByRole("article", { name: "用户消息" })).toBeDefined();
  });

  it("两种形态下输入框都在，且是同一个（不会因为切换而丢掉草稿）", () => {
    const empty = renderView(makeController());
    const heroBox = empty.getByRole("textbox", { name: "消息输入框" });
    expect(heroBox).toBeDefined();
    cleanup();

    const started = renderView(makeController({ groups: [humanGroup] }));
    expect(started.getByRole("textbox", { name: "消息输入框" })).toBeDefined();
  });

  it("有产物时才渲染产物面板", () => {
    const view = renderView(makeController({ groups: [humanGroup], artifacts: ["a.txt"] }));

    expect(view.queryByText("这一轮还没有产物。")).toBeNull();
    expect(view.getByRole("complementary", { name: "产物" })).toBeDefined();
  });
});
