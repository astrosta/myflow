import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Composer } from "./composer";

/**
 * 发送按钮的"点亮"是**纯客户端**行为：空输入时禁用灰底，一打字就变实心深色。
 * SSR 出来的 HTML 只能证明初始态，所以这条必须在真实 React 下测
 * （M5 §8.1：只在真实 React 里存在的行为，不要写成 mock 掉 React 的 node 测试）。
 */

afterEach(cleanup);

/**
 * 不用 jest-dom 的 toBeDisabled：这个仓库没装它。
 * 原生 disabled 属性本来就够断言，也少一个依赖。
 */
const isDisabled = (element: HTMLElement) => (element as HTMLButtonElement).disabled;

function setup(props: Partial<React.ComponentProps<typeof Composer>> = {}) {
  const onSubmit = vi.fn(async () => {});
  const view = render(<Composer onSubmit={onSubmit} {...props} />);
  const textbox = view.getByRole("textbox", { name: "消息输入框" });
  return { view, onSubmit, textbox };
}

describe("发送按钮的状态", () => {
  it("空输入时是禁用的灰底圆钮", () => {
    const { view } = setup();
    const send = view.getByRole("button", { name: "发送" });

    expect(isDisabled(send)).toBe(true);
    expect(send.className).toContain("bg-neutral-200");
    expect(send.className).not.toContain("bg-neutral-900");
  });

  it("**用户一打字就高亮**", () => {
    const { view, textbox } = setup();
    fireEvent.change(textbox, { target: { value: "你好" } });

    const send = view.getByRole("button", { name: "发送" });
    expect(isDisabled(send)).toBe(false);
    expect(send.className).toContain("bg-neutral-900");
    expect(send.className).not.toContain("bg-neutral-200");
  });

  it("只有空白字符不算内容", () => {
    const { view, textbox } = setup();
    fireEvent.change(textbox, { target: { value: "   " } });

    expect(isDisabled(view.getByRole("button", { name: "发送" }))).toBe(true);
  });

  it("发出去之后回到灰底", async () => {
    const { view, textbox } = setup();
    fireEvent.change(textbox, { target: { value: "你好" } });
    fireEvent.click(view.getByRole("button", { name: "发送" }));

    await vi.waitFor(() => {
      expect(isDisabled(view.getByRole("button", { name: "发送" }))).toBe(true);
    });
  });

  it("按钮是箭头图标，没有文字", () => {
    const { view } = setup();
    const send = view.getByRole("button", { name: "发送" });

    // 可访问名来自 aria-label：e2e 和读屏都靠它，换成图标之后不能丢
    expect(send.textContent).toBe("");
    expect(send.querySelector("svg")).not.toBeNull();
  });

  it("流式期间发送换成停止，两个都是图标按钮", () => {
    const { view } = setup({ streaming: true, onStop: () => {} });

    expect(view.queryByRole("button", { name: "发送" })).toBeNull();
    const stop = view.getByRole("button", { name: "停止" });
    expect(stop.querySelector("svg")).not.toBeNull();
  });
});

describe("输入框是一个整体，按钮在里面右下角", () => {
  it("整个框在聚焦时高亮（而不是只有 textarea 那一像素边框）", () => {
    const { textbox } = setup();
    // textarea 的直接父节点就是这个带边框的框
    const box = textbox.parentElement!;

    expect(box.className).toContain("focus-within:border-neutral-500");
    expect(box.className).toContain("focus-within:ring-2");
    // 边框本身也要在,否则 focus-within 改的是个不存在的边框
    expect(box.className).toContain("border-neutral-300");
  });

  it("textarea 融进框里：自己不带边框", () => {
    const { textbox } = setup();

    expect(textbox.className).toContain("border-0");
    expect(textbox.className).toContain("bg-transparent");
  });

  it("**发送按钮在框内部**，不是挂在框外面", () => {
    const { view, textbox } = setup();
    const box = textbox.parentElement!;
    const send = view.getByRole("button", { name: "发送" });

    expect(box.contains(send)).toBe(true);
  });

  it("按钮落在工具行的**最右边**（右下角）", () => {
    const { view, textbox } = setup();
    const toolbar = textbox.parentElement!.lastElementChild!;
    const send = view.getByRole("button", { name: "发送" });

    expect(toolbar.contains(send)).toBe(true);
    // 工具行的最后一个子节点就是按钮所在的那个容器
    expect(toolbar.lastElementChild!.contains(send)).toBe(true);
  });

  it("附件按钮和发送按钮在同一行，附件在左", () => {
    const { view, textbox } = setup();
    const toolbar = textbox.parentElement!.lastElementChild!;
    const send = view.getByRole("button", { name: "发送" });
    const attach = toolbar.querySelector("button, label, input[type=file]");

    expect(attach).not.toBeNull();
    // 附件在文档顺序上先于发送
    expect(
      attach!.compareDocumentPosition(send) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});

describe("聚焦外观", () => {
  it("**不再出现黑色的 :focus-visible 矩形框**", () => {
    const { textbox } = setup();

    // globals.css 里有一条全局的 :focus-visible { outline: 2px solid var(--color-accent) }，
    // --color-accent 是 #1f2937，聚焦时会在光标周围画一个近黑色的矩形。
    // 输入框必须显式关掉它 —— 可访问性由外层容器的 focus-within 边框承担。
    expect(textbox.className).toContain("focus-visible:outline-none");
  });

  it("关掉 outline 之后仍然有可见的聚焦指示（容器的 focus-within）", () => {
    const { textbox } = setup();
    const box = textbox.parentElement!;

    expect(box.className).toContain("focus-within:border-neutral-500");
  });
});

describe("发送失败要说清楚", () => {
  it("把原因整行显示在输入框上方，而不是「点了没反应」", async () => {
    const onSubmit = vi.fn(async () => {
      throw new Error("创建会话失败：HTTP 404");
    });
    const view = render(<Composer onSubmit={onSubmit} />);
    const textbox = view.getByRole("textbox", { name: "消息输入框" });

    fireEvent.change(textbox, { target: { value: "你好" } });
    fireEvent.click(view.getByRole("button", { name: "发送" }));

    const alert = await view.findByRole("alert");
    expect(alert.textContent).toContain("发送失败");
    expect(alert.textContent).toContain("HTTP 404");
    // 整行显示，不截断
    expect(alert.className).not.toContain("truncate");
    // 在输入框容器之外（上方），不跟附件、发送挤在一行
    expect(textbox.parentElement!.contains(alert)).toBe(false);
  });

  it("失败后输入内容还在，用户不用重打", async () => {
    const onSubmit = vi.fn(async () => {
      throw new Error("boom");
    });
    const view = render(<Composer onSubmit={onSubmit} />);
    const textbox = view.getByRole("textbox", { name: "消息输入框" }) as HTMLTextAreaElement;

    fireEvent.change(textbox, { target: { value: "别弄丢我" } });
    fireEvent.click(view.getByRole("button", { name: "发送" }));
    await view.findByRole("alert");

    expect(textbox.value).toBe("别弄丢我");
  });
});

describe("键盘行为没有被图标化改动", () => {
  it("Enter 发送", async () => {
    const { onSubmit, textbox } = setup();
    fireEvent.change(textbox, { target: { value: "你好" } });
    fireEvent.keyDown(textbox, { key: "Enter" });

    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledWith("你好", []));
  });

  it("Shift+Enter 不发送（换行）", () => {
    const { onSubmit, textbox } = setup();
    fireEvent.change(textbox, { target: { value: "你好" } });
    fireEvent.keyDown(textbox, { key: "Enter", shiftKey: true });

    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("中文输入法选词时的回车不发送", () => {
    const { onSubmit, textbox } = setup();
    fireEvent.change(textbox, { target: { value: "你好" } });
    fireEvent.keyDown(textbox, { key: "Enter", isComposing: true });

    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("两种形态只改观感", () => {
  it("hero 形态下仍然能发送", async () => {
    const { onSubmit, view, textbox } = setup({ variant: "hero" });
    fireEvent.change(textbox, { target: { value: "你好" } });
    fireEvent.click(view.getByRole("button", { name: "发送" }));

    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledWith("你好", []));
  });

  it("hero 形态没有顶部分隔线，docked 有", () => {
    const hero = render(<Composer onSubmit={async () => {}} variant="hero" />);
    expect(hero.container.querySelector("form")!.className).not.toContain("border-t");
    cleanup();

    const docked = render(<Composer onSubmit={async () => {}} />);
    expect(docked.container.querySelector("form")!.className).toContain("border-t");
  });
});
