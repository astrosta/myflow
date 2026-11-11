import { cleanup, render, screen } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import LandingPage from "./page";

vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...rest
  }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; children: ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock("@/components/auth/home-auth-link", () => ({
  HomeAuthLink: () => null,
}));

afterEach(cleanup);

describe("首页", () => {
  it("用一句话讲清越用越懂你的定位", () => {
    render(<LandingPage />);

    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
      "越用越懂你的智能体——它记得你们的每一次协作，并不断把经验转化为更贴合你的能力。",
    );
    expect(screen.getByRole("link", { name: "进入工作区" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "跳到主要内容" }).getAttribute("href")).toBe(
      "#main-content",
    );
  });

  it("五种能力各有一句话介绍", () => {
    render(<LandingPage />);

    const articles = screen.getAllByRole("article");
    expect(articles).toHaveLength(5);

    for (const title of ["记忆管理", "技能中心", "工具管理", "知识库", "可评测、可观测"]) {
      expect(screen.getByRole("heading", { level: 2, name: title })).toBeTruthy();
    }

    for (const article of articles) {
      expect(article.querySelector("p")?.textContent?.trim()).toMatch(/。$/);
    }
  });

  it("不再渲染说明段落、能力锚点和产品原则", () => {
    render(<LandingPage />);

    expect(screen.queryByText("查看五种能力")).toBeNull();
    expect(screen.queryByText(/MyFlow 是自托管/)).toBeNull();
    expect(screen.queryByText("从记得你，到更懂你。")).toBeNull();
    expect(screen.queryByText(/底线很简单/)).toBeNull();
  });
});
