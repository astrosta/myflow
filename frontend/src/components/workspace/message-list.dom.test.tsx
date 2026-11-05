import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import type { MessageGroup } from "@/core/messages/group";

import { MessageList } from "./message-list";

/**
 * 消息列表的"打开会话就落到最新一条"。
 *
 * happy-dom 没有布局，scrollHeight / clientHeight 恒为 0；这里给滚动容器补一份
 * 固定的假尺寸，让 effect 里的"是否在底部"判断真正可测。真实浏览器会把
 * scrollTop clamp 到 scrollHeight - clientHeight，happy-dom 不做这个 clamp，
 * 所以断言写"至少到底部上限"，而不是一个特定值。
 */
const SCROLL_HEIGHT = 1000;
const CLIENT_HEIGHT = 400;
const MAX_SCROLL_TOP = SCROLL_HEIGHT - CLIENT_HEIGHT;

const originalScrollHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollHeight");
const originalClientHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "clientHeight");

beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains("overflow-y-auto") ? SCROLL_HEIGHT : 0;
    },
  });
  Object.defineProperty(HTMLElement.prototype, "clientHeight", {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains("overflow-y-auto") ? CLIENT_HEIGHT : 0;
    },
  });
});

afterAll(() => {
  if (originalScrollHeight) {
    Object.defineProperty(HTMLElement.prototype, "scrollHeight", originalScrollHeight);
  } else {
    Reflect.deleteProperty(HTMLElement.prototype, "scrollHeight");
  }
  if (originalClientHeight) {
    Object.defineProperty(HTMLElement.prototype, "clientHeight", originalClientHeight);
  } else {
    Reflect.deleteProperty(HTMLElement.prototype, "clientHeight");
  }
});

afterEach(cleanup);

const firstGroup: MessageGroup = {
  kind: "human",
  id: "human-u1",
  message: { id: "u1", type: "human", content: "你好" },
};

const secondGroup: MessageGroup = {
  kind: "human",
  id: "human-u2",
  message: { id: "u2", type: "human", content: "再问一句" },
};

function viewportOf(container: HTMLElement): HTMLElement {
  const element = container.querySelector<HTMLElement>(".overflow-y-auto");
  if (!element) throw new Error("没找到消息滚动容器");
  return element;
}

describe("打开会话时消息列表的位置", () => {
  it("首帧就带着缓存历史，也必须落在最新一条上", () => {
    const view = render(<MessageList groups={[firstGroup]} />);

    // 回归：以前挂载时会立刻跑一次 scroll 判定。首帧 scrollTop=0 且内容比视口高，
    // 于是 pinned 被误判成 false，groups effect 再也不会滚到底 —— 点回一个
    // 已经打开过的会话会停在最上面。
    expect(viewportOf(view.container).scrollTop).toBeGreaterThanOrEqual(MAX_SCROLL_TOP);
  });

  it("用户往上翻之后，新消息不会把视口拽回底部", () => {
    const view = render(<MessageList groups={[firstGroup]} />);
    const viewport = viewportOf(view.container);

    viewport.scrollTop = 0;
    fireEvent.scroll(viewport);
    view.rerender(<MessageList groups={[firstGroup, secondGroup]} />);

    expect(viewport.scrollTop).toBe(0);
  });

  it("点当前会话标题（request 变化）才强制回到最新一条", () => {
    const view = render(<MessageList groups={[firstGroup]} scrollToBottomRequest={0} />);
    const viewport = viewportOf(view.container);

    viewport.scrollTop = 0;
    fireEvent.scroll(viewport);

    view.rerender(<MessageList groups={[firstGroup]} scrollToBottomRequest={1} />);

    expect(viewport.scrollTop).toBeGreaterThanOrEqual(MAX_SCROLL_TOP);
  });
});
