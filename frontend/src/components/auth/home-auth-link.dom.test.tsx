import { cleanup, render } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSession, type SessionState } from "@/core/auth/use-session";

import { HomeAuthLink } from "./home-auth-link";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: AnchorHTMLAttributes<HTMLAnchorElement> & { href: string; children: ReactNode }) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock("@/core/auth/use-session", () => ({ useSession: vi.fn() }));

const anonymousSession: SessionState = {
  user: null,
  status: "anonymous",
  loading: false,
  refresh: vi.fn(),
  signOut: vi.fn(),
};

const authenticatedSession: SessionState = {
  user: {
    id: "u1",
    email: null,
    username: "alice",
    system_role: "user",
  },
  status: "authenticated",
  loading: false,
  refresh: vi.fn(),
  signOut: vi.fn(),
};

beforeEach(() => {
  vi.mocked(useSession).mockReset();
  vi.mocked(useSession).mockReturnValue(anonymousSession);
});

afterEach(cleanup);

describe("首页登录入口", () => {
  it("未登录时显示登录入口", () => {
    const view = render(<HomeAuthLink />);
    const login = view.getByRole("link", { name: "登录" });

    expect(login.getAttribute("href")).toBe("/login");
  });

  it("已登录时不渲染登录入口，右上角只保留工作区", () => {
    vi.mocked(useSession).mockReturnValue(authenticatedSession);
    const view = render(<HomeAuthLink />);

    expect(view.queryByRole("link", { name: "登录" })).toBeNull();
    expect(view.container.textContent).toBe("");
  });
});
