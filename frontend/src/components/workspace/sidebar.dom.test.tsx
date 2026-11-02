import { cleanup, render } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ThreadSummary } from "@/core/types";

import { Sidebar } from "./sidebar";

/**
 * 侧栏版面：品牌行 → 新对话 → 知识库 → **最近对话**（历史会话挂在它下面）。
 *
 * `next/link` 在 vitest 里没有路由上下文，这里替换成一个普通 `<a>`：
 * 这条测试要断言的是**结构和文案**，不是 Next 的路由行为。
 */
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; children: ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

afterEach(cleanup);

function makeThread(id: string, title: string): ThreadSummary {
  return {
    thread_id: id,
    display_name: title,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    metadata: {},
  };
}

describe("侧栏的最近对话栏", () => {
  it("历史会话藏在一个叫「最近对话」的导航分区里，不是直接铺在侧栏上", () => {
    const view = render(
      <Sidebar threads={[makeThread("t1", "第一个会话"), makeThread("t2", "第二个会话")]} />,
    );

    const nav = view.getByRole("navigation", { name: "最近对话" });
    expect(nav.textContent).toContain("最近对话");
    expect(nav.textContent).toContain("第一个会话");
    expect(nav.textContent).toContain("第二个会话");
    // 会话是列表项：不是一串散落的链接
    expect(view.getAllByRole("list").length).toBe(1);
    expect(view.getAllByRole("listitem").length).toBe(2);
  });

  it("分区标题在会话列表之上，「新对话」和「知识库」又在标题之上", () => {
    const view = render(<Sidebar threads={[makeThread("t1", "第一个会话")]} />);

    const heading = view.getByRole("heading", { name: /最近对话/ });
    const link = view.getByRole("link", { name: /第一个会话/ });
    const knowledge = view.getByRole("link", { name: /知识库/ });
    const newChat = view.getByRole("link", { name: /新对话/ });

    // compareDocumentPosition：FOLLOWING = 4，说明 heading 在 link 之前
    expect(heading.compareDocumentPosition(link) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(newChat.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(knowledge.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("没有会话时标题照样在，只多一句引导文案", () => {
    const view = render(<Sidebar threads={[]} />);

    expect(view.getByRole("heading", { name: /最近对话/ })).toBeTruthy();
    expect(view.getByText(/还没有会话/)).toBeTruthy();
  });

  it("导航里有「记忆管理」，按当前路由高亮", () => {
    const view = render(<Sidebar memoryActive threads={[]} />);

    const memory = view.getByRole("link", { name: /记忆管理/ });
    expect(memory.getAttribute("href")).toBe("/workspace/memory");
    expect(memory.getAttribute("aria-current")).toBe("page");
    expect(view.getByRole("link", { name: /我的画像/ }).getAttribute("aria-current")).toBeNull();
  });

  it("当前会话高亮：aria-current 只挂在它自己身上", () => {
    const view = render(
      <Sidebar activeThreadId="t2" threads={[makeThread("t1", "第一个会话"), makeThread("t2", "第二个会话")]} />,
    );

    expect(view.getByRole("link", { name: /第二个会话/ }).getAttribute("aria-current")).toBe("page");
    expect(view.getByRole("link", { name: /第一个会话/ }).getAttribute("aria-current")).toBeNull();
  });
});
