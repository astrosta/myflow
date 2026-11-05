import { cleanup, render } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSession, type SessionState } from "@/core/auth/use-session";

import { formatHomeUsername, HomeAuthLink } from "./home-auth-link";

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

function authenticatedSession(username: string): SessionState {
  return {
    user: {
      id: "u1",
      email: null,
      username,
      system_role: "user",
    },
    status: "authenticated",
    loading: false,
    refresh: vi.fn(),
    signOut: vi.fn(),
  };
}

beforeEach(() => {
  vi.mocked(useSession).mockReset();
  vi.mocked(useSession).mockReturnValue(anonymousSession);
});

afterEach(cleanup);

describe("首页会话入口", () => {
  it("未登录时显示登录入口", () => {
    const view = render(<HomeAuthLink />);
    const login = view.getByRole("link", { name: "登录" });

    expect(login.getAttribute("href")).toBe("/login");
  });

  it("已登录时显示用户名并进入工作区", () => {
    vi.mocked(useSession).mockReturnValue(authenticatedSession("alice"));
    const view = render(<HomeAuthLink />);
    const profile = view.getByRole("link", { name: "alice" });

    expect(profile.getAttribute("href")).toBe("/workspace");
    expect(profile.textContent).toBe("alice");
  });

  it("超长用户名截断后补 ***，完整名字留在 title 和 aria-label 里", () => {
    vi.mocked(useSession).mockReturnValue(authenticatedSession("abcdefghijklmnop"));
    const view = render(<HomeAuthLink />);
    const profile = view.getByRole("link", { name: "abcdefghijklmnop" });

    expect(profile.textContent).toBe("abcdefghijkl***");
    expect(profile.getAttribute("title")).toBe("abcdefghijklmnop");
  });

  it("登录入口宽度固定，长短用户名不会改变导航布局", () => {
    vi.mocked(useSession).mockReturnValue(authenticatedSession("a".repeat(32)));
    const view = render(<HomeAuthLink />);

    expect(view.getByRole("link", { name: "a".repeat(32) }).className).toContain("w-32");
  });

  it("formatHomeUsername 只处理超长用户名", () => {
    expect(formatHomeUsername("alice")).toBe("alice");
    expect(formatHomeUsername("abcdefghijkl")).toBe("abcdefghijkl");
    expect(formatHomeUsername("abcdefghijklm")).toBe("abcdefghijkl***");
  });
});
