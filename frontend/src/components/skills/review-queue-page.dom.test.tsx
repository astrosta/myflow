import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  approveReview,
  getReview,
  listReviews,
  rejectReview,
  SkillApiError,
  type ReviewDetail as ReviewDetailPayload,
  type ReviewSummary,
} from "@/core/api/skills";

import { ReviewQueuePage } from "./review-queue-page";

import { SidebarContainer } from "@/app/workspace/sidebar-container";

/**
 * 管理端审核台 + 侧栏入口。
 *
 * 这里测两件事：
 * 1. **侧栏的「审核」入口按会话角色显隐** —— 走真实的 SidebarContainer，
 *    只把 useSession 换成可控的假会话。隐藏入口只是**展示层**的选择，
 *    真正的闸门是后端的 admin scope（page.tsx 的注释里也写了同一句话）。
 * 2. **审核队列的装配** —— 取数、待审数量、详情入口、通过/驳回的调用与
 *    错误渲染。API 模块整体换成 vi.fn()，只断言"调没调对、画成什么样"。
 */

let currentUser: { id: string; email: string; username: string; system_role: "admin" | "user" } | null =
  null;

vi.mock("@/core/auth/use-session", () => ({
  useSession: () => ({
    user: currentUser,
    status: "authenticated",
    loading: false,
    refresh: async () => {},
    signOut: async () => {},
  }),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/workspace/skills",
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    back: vi.fn(),
    refresh: vi.fn(),
    prefetch: vi.fn(),
  }),
}));

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

vi.mock("@/app/workspace/chat-session", () => ({
  useChatSession: () => ({ activeThreadId: null, newChat: vi.fn() }),
}));

vi.mock("@/core/api/threads", () => ({
  listThreads: vi.fn(async () => ({ data: [], next_cursor: null, has_more: false })),
}));

vi.mock("@/core/api/skills", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/skills")>();
  return {
    ...actual,
    listReviews: vi.fn(),
    getReview: vi.fn(),
    approveReview: vi.fn(),
    rejectReview: vi.fn(),
  };
});

const mockedListReviews = vi.mocked(listReviews);
const mockedGetReview = vi.mocked(getReview);
const mockedApprove = vi.mocked(approveReview);
const mockedReject = vi.mocked(rejectReview);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

beforeEach(() => {
  currentUser = null;
});

/** 每个用例一份新的 QueryClient：缓存不跨用例，失败也不重试（错误态要立刻可见）。 */
function renderWithClient(node: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(<QueryClientProvider client={client}>{node}</QueryClientProvider>);
}

function makeSummary(overrides: Partial<ReviewSummary> = {}): ReviewSummary {
  return {
    submission_id: "sub-1",
    skill_id: "skill-1",
    name: "日报生成",
    version: "1.0.0",
    owner_user_id: "u-alice",
    submitted_at: "2026-10-20T08:00:00Z",
    submitted_commit: "abcdef1234567890",
    ...overrides,
  };
}

function makeDetail(overrides: Partial<ReviewDetailPayload> = {}): ReviewDetailPayload {
  return {
    ...makeSummary(),
    files: [
      { path: "SKILL.md", size_bytes: 128, mode: "text", sha256: "a".repeat(64) },
      { path: "scripts/run.py", size_bytes: 2048, mode: "executable", sha256: "b".repeat(64) },
    ],
    skill_md: "# 日报生成\n\n把今天的记录整理成日报。",
    diff: "+ 新增 scripts/run.py\n+ 新增 SKILL.md",
    metadata: { description: "整理日报" },
    package_bytes: 2176,
    ...overrides,
  };
}

describe("侧栏的技能 / 审核入口", () => {
  it("普通用户看得到「技能」，看不到「审核」", async () => {
    currentUser = { id: "u-alice", email: "alice@example.com", username: "alice", system_role: "user" };

    renderWithClient(<SidebarContainer />);

    const skills = await screen.findByRole("link", { name: /技能/ });
    expect(skills.getAttribute("href")).toBe("/workspace/skills");
    // 隐藏入口是展示层的事：真正的闸门在后端 admin scope（见 sidebar.tsx 的注释）
    expect(screen.queryByRole("link", { name: /审核/ })).toBeNull();
  });

  it("管理员多一个「审核」入口，指向 /workspace/admin/skills", async () => {
    currentUser = { id: "u-root", email: "root@example.com", username: "root", system_role: "admin" };

    renderWithClient(<SidebarContainer />);

    const review = await screen.findByRole("link", { name: /审核/ });
    expect(review.getAttribute("href")).toBe("/workspace/admin/skills");
  });
});

describe("技能审核队列", () => {
  it("列出待审核提交、待审数量和详情入口", async () => {
    mockedListReviews.mockResolvedValue([makeSummary()]);

    renderWithClient(<ReviewQueuePage />);

    expect(await screen.findByText("日报生成")).toBeTruthy();
    expect(screen.getByText(/待审核 1/)).toBeTruthy();
    // 详情入口是真链接：href 里带 submission_id，点开在同一页展开详情
    const detail = screen.getByRole("link", { name: /查看详情/ });
    expect(detail.getAttribute("href")).toContain("sub-1");
  });

  it("详情展示作者、版本、提交 commit、文件树、SKILL.md、diff 和包大小", async () => {
    mockedListReviews.mockResolvedValue([makeSummary()]);
    mockedGetReview.mockResolvedValue(makeDetail());

    renderWithClient(<ReviewQueuePage />);

    fireEvent.click(await screen.findByRole("link", { name: /查看详情/ }));

    await waitFor(() => expect(mockedGetReview).toHaveBeenCalledWith("sub-1"));
    expect(await screen.findByText(/u-alice/)).toBeTruthy();
    expect(screen.getByText(/1\.0\.0/)).toBeTruthy();
    // commit 只显示前 12 位：完整哈希在 title 里
    expect(screen.getByText("abcdef123456")).toBeTruthy();
    expect(screen.getByText("scripts/run.py")).toBeTruthy();
    expect(screen.getByText(/把今天的记录整理成日报/)).toBeTruthy();
    expect(screen.getByText(/新增 scripts\/run\.py/)).toBeTruthy();
    expect(screen.getByText(/2\.1 KB/)).toBeTruthy();
  });

  it("「通过」调用 approveReview，之后这一行从队列里消失", async () => {
    mockedListReviews.mockResolvedValueOnce([makeSummary()]).mockResolvedValue([]);
    mockedGetReview.mockResolvedValue(makeDetail());
    mockedApprove.mockResolvedValue({
      release_id: "rel-1",
      skill_id: "skill-1",
      skill_version: "1.0.0",
      git_repository: "skills",
      git_commit: "abcdef1234567890",
      skill_path: "skills/daily",
      public_name: "daily-report",
      approved_by: "u-root",
      created_at: "2026-10-21T00:00:00Z",
    });

    renderWithClient(<ReviewQueuePage />);

    fireEvent.click(await screen.findByRole("link", { name: /查看详情/ }));
    fireEvent.click(await screen.findByRole("button", { name: "通过" }));

    await waitFor(() => expect(mockedApprove).toHaveBeenCalledWith("sub-1"));
    // 失效 ["skills","reviews"] 之后重新取数，队列里没有这一条了
    await waitFor(() => expect(screen.queryByText("日报生成")).toBeNull());
    expect(await screen.findByText("没有待审核的提交")).toBeTruthy();
  });

  it("驳回必须填理由：空理由不发请求", async () => {
    mockedListReviews.mockResolvedValue([makeSummary()]);
    mockedGetReview.mockResolvedValue(makeDetail());

    renderWithClient(<ReviewQueuePage />);

    fireEvent.click(await screen.findByRole("link", { name: /查看详情/ }));
    fireEvent.click(await screen.findByRole("button", { name: "驳回" }));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("理由"));
    expect(mockedReject).not.toHaveBeenCalled();
  });

  it("填了理由再驳回：rejectReview 收到理由，行被移除", async () => {
    mockedListReviews.mockResolvedValueOnce([makeSummary()]).mockResolvedValue([]);
    mockedGetReview.mockResolvedValue(makeDetail());
    mockedReject.mockResolvedValue({
      submission_id: "sub-1",
      skill_id: "skill-1",
      version_id: "ver-1",
      status: "rejected",
      submitted_at: "2026-10-20T08:00:00Z",
      owner_user_id: "u-alice",
    });

    renderWithClient(<ReviewQueuePage />);

    fireEvent.click(await screen.findByRole("link", { name: /查看详情/ }));
    fireEvent.change(await screen.findByLabelText("驳回理由"), {
      target: { value: "SKILL.md 缺少触发条件" },
    });
    fireEvent.click(screen.getByRole("button", { name: "驳回" }));

    await waitFor(() => expect(mockedReject).toHaveBeenCalledWith("sub-1", "SKILL.md 缺少触发条件"));
    await waitFor(() => expect(screen.queryByText("日报生成")).toBeNull());
  });

  it("通过失败时渲染一条人读的告警", async () => {
    mockedListReviews.mockResolvedValue([makeSummary()]);
    mockedGetReview.mockResolvedValue(makeDetail());
    mockedApprove.mockRejectedValue(
      new SkillApiError(409, "review_conflict", "这一版已经审过了", null),
    );

    renderWithClient(<ReviewQueuePage />);

    fireEvent.click(await screen.findByRole("link", { name: /查看详情/ }));
    fireEvent.click(await screen.findByRole("button", { name: "通过" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("这一版已经审过了");
  });

  it("驳回失败时也渲染告警，且队列里的行还在", async () => {
    mockedListReviews.mockResolvedValue([makeSummary()]);
    mockedGetReview.mockResolvedValue(makeDetail());
    mockedReject.mockRejectedValue(
      new SkillApiError(500, "internal_error", "驳回失败，请稍后再试", null),
    );

    renderWithClient(<ReviewQueuePage />);

    fireEvent.click(await screen.findByRole("link", { name: /查看详情/ }));
    fireEvent.change(await screen.findByLabelText("驳回理由"), { target: { value: "不符合规范" } });
    fireEvent.click(screen.getByRole("button", { name: "驳回" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("驳回失败，请稍后再试");
    expect(screen.queryByText("没有待审核的提交")).toBeNull();
  });
});
