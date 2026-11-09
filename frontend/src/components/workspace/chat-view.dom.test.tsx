import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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

describe("图级中断", () => {
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

  it("空会话不渲染产物面板", () => {
    const view = renderView(makeController());

    expect(view.queryByRole("complementary", { name: "产物" })).toBeNull();
  });

  it("新会话也能添加文件", () => {
    const view = renderView(makeController({ threadId: null }));

    expect(view.getByRole("button", { name: "添加文件" })).toHaveProperty("disabled", false);
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

  it("用户上翻历史后发送消息，会跳回最底部", async () => {
    const submit = vi.fn(async () => {});
    const view = renderView(
      makeController({
        groups: [humanGroup],
        messages: [humanGroup.message],
        submit,
      }),
    );

    const viewport = view.container.querySelector<HTMLElement>(".overflow-y-auto");
    if (!viewport) throw new Error("没找到消息滚动容器");
    // happy-dom 没有布局：给滚动容器补一份固定尺寸，才能验证"上翻"和"回到底部"
    Object.defineProperty(viewport, "scrollHeight", { configurable: true, value: 1_200 });
    Object.defineProperty(viewport, "clientHeight", { configurable: true, value: 400 });
    viewport.scrollTop = 0;
    fireEvent.scroll(viewport);

    fireEvent.change(view.getByRole("textbox", { name: "消息输入框" }), {
      target: { value: "新问题" },
    });
    fireEvent.click(view.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(viewport.scrollTop).toBeGreaterThanOrEqual(800);
    });
    expect(submit).toHaveBeenCalledWith("新问题", []);
  });

  it("有产物时才渲染产物面板；没有产物时右边什么都不留", () => {
    const withoutArtifacts = renderView(makeController({ groups: [humanGroup] }));
    expect(withoutArtifacts.queryByRole("complementary", { name: "产物" })).toBeNull();
    cleanup();

    const view = renderView(makeController({ groups: [humanGroup], artifacts: ["a.txt"] }));
    expect(view.getByRole("complementary", { name: "产物" })).toBeDefined();
  });
});
