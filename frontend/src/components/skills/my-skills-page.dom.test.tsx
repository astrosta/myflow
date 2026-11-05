import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createSkill,
  listMySkills,
  listSkills,
  setSkillPreference,
  SkillApiError,
  type SkillListItem,
} from "@/core/api/skills";

import { MySkillsPage } from "./my-skills-page";

/**
 * 「我的技能」页：只展示当前用户自己的技能 + 内置技能。
 * API 模块整体换成 vi.fn()，断言页面有没有按约定调用、拿到数据后画成什么样。
 */

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push,
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

vi.mock("@/core/api/skills", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/skills")>();
  return {
    ...actual,
    listMySkills: vi.fn(),
    listSkills: vi.fn(),
    createSkill: vi.fn(),
    setSkillPreference: vi.fn(),
  };
});

const mockedListMine = vi.mocked(listMySkills);
const mockedListCatalog = vi.mocked(listSkills);
const mockedCreate = vi.mocked(createSkill);
const mockedSetPreference = vi.mocked(setSkillPreference);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function makeSkill(overrides: Partial<SkillListItem> = {}): SkillListItem {
  return {
    skill_id: "skill-1",
    id: "skill-1",
    name: "日报生成",
    description: "整理日报",
    scope: "private",
    owner_user_id: "u-alice",
    skill_version: "v1",
    version_id: "ver-1",
    git_commit: "abc",
    skill_path: "users/u-alice/skill-1/SKILL.md",
    status: "scanned",
    enabled: false,
    container_file: null,
    index_status: "unindexed",
    active_version_id: null,
    active: false,
    latest_scan: {
      scan_id: "scan-1",
      version_id: "ver-1",
      status: "completed",
      is_safe: true,
      max_severity: "SAFE",
      findings_count: 0,
      scanner_version: "2.3.2",
      duration_ms: 10,
      created_at: "2026-11-02T00:00:00Z",
    },
    can_activate: true,
    activation_blocked_reason: null,
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MySkillsPage />
    </QueryClientProvider>,
  );
}

describe("我的技能页", () => {
  it("空列表显示创建入口和扫描说明", async () => {
    mockedListMine.mockResolvedValue([]);
    mockedListCatalog.mockResolvedValue([]);

    renderPage();

    expect(await screen.findByText("还没有技能")).toBeTruthy();
    expect(screen.getByText(/保存后先做安全扫描/)).toBeTruthy();
    expect(screen.getAllByRole("button", { name: "新建技能" }).length).toBeGreaterThan(0);
  });

  it("生效技能归入「我的技能」，草稿归入上方的「技能草稿」", async () => {
    mockedListMine.mockResolvedValue([
      makeSkill({
        skill_id: "draft-1",
        name: "草稿技能",
        status: "scanned",
        active: false,
        index_status: "unindexed",
      }),
      makeSkill({
        skill_id: "active-1",
        name: "生效技能",
        status: "active",
        active: true,
        enabled: true,
        index_status: "disabled",
      }),
    ]);
    mockedListCatalog.mockResolvedValue([]);

    renderPage();

    expect(screen.getByRole("heading", { name: "技能中心" })).toBeTruthy();
    const drafts = await screen.findByRole("region", { name: "技能草稿" });
    const active = screen.getByRole("region", { name: "我的技能" });
    expect(
      drafts.compareDocumentPosition(active) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(within(drafts).getByText("草稿技能")).toBeTruthy();
    expect(within(active).getByText("生效技能")).toBeTruthy();
    // 「未启用」保留索引语义；已索引 / 索引中 / 未索引 不再重复加前缀
    expect(within(active).getByText(/索引未启用/)).toBeTruthy();

    const toggle = within(active).getByRole("switch", { name: "停用技能" });
    expect(toggle.getAttribute("aria-checked")).toBe("true");
    mockedSetPreference.mockResolvedValue(makeSkill({ enabled: false }));
    fireEvent.click(toggle);
    await waitFor(() =>
      expect(mockedSetPreference).toHaveBeenCalledWith("active-1", false),
    );
  });

  it("展示生命周期、扫描摘要和编辑入口", async () => {
    mockedListMine.mockResolvedValue([makeSkill()]);
    mockedListCatalog.mockResolvedValue([]);

    renderPage();

    expect(await screen.findByText("日报生成")).toBeTruthy();
    expect(screen.getByText("草稿 · 扫描通过")).toBeTruthy();
    expect(screen.getByText(/安全扫描：通过/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    expect(push).toHaveBeenCalledWith("/workspace/skills/skill-1");
  });

  it("提炼技能显示标签并停在待扫描状态", async () => {
    mockedListMine.mockResolvedValue([
      makeSkill({
        name: "session-helper",
        description: "从会话提炼",
        source: "extracted",
        source_uri: "viking://user/u1/skills/session-helper",
        status: "unscanned",
        latest_scan: null,
        can_activate: false,
      }),
    ]);
    mockedListCatalog.mockResolvedValue([]);

    renderPage();

    expect(await screen.findByText("session-helper")).toBeTruthy();
    expect(screen.getByText("提炼")).toBeTruthy();
    expect(screen.getByText("草稿 · 待扫描")).toBeTruthy();
  });

  it("只把内置技能放进内置区，不渲染公共目录", async () => {
    mockedListMine.mockResolvedValue([]);
    mockedListCatalog.mockResolvedValue([
      makeSkill({
        skill_id: "builtin-1",
        name: "内置搜索",
        scope: "builtin",
        status: "builtin",
        owner_user_id: null,
        latest_scan: null,
        can_activate: false,
      }),
    ]);

    renderPage();

    expect(await screen.findByText("内置搜索")).toBeTruthy();
    expect(screen.getByText("内置技能")).toBeTruthy();
    expect(screen.queryByText("公共技能")).toBeNull();
  });

  it("接口错误直接回显 detail", async () => {
    mockedListMine.mockRejectedValue(
      new SkillApiError(503, "skill_runtime_unavailable", "技能运行时未配置", null),
    );
    mockedListCatalog.mockResolvedValue([]);

    renderPage();

    expect((await screen.findByRole("alert")).textContent).toContain("技能运行时未配置");
  });

  it("新建技能不弹表单，直接创建并跳到编辑器", async () => {
    mockedListMine.mockResolvedValue([]);
    mockedListCatalog.mockResolvedValue([]);
    mockedCreate.mockResolvedValue(makeSkill({ status: "draft", skill_version: null }));

    renderPage();
    fireEvent.click((await screen.findAllByRole("button", { name: "新建技能" }))[0]!);

    expect(screen.queryByRole("dialog", { name: "新建技能" })).toBeNull();
    await waitFor(() =>
      expect(mockedCreate).toHaveBeenCalledWith({
        name: expect.stringMatching(/^new-skill-[a-z0-9]+$/),
        description: "在 SKILL.md 中填写技能说明",
      }),
    );
    expect(push).toHaveBeenCalledWith("/workspace/skills/skill-1");
  });
});
