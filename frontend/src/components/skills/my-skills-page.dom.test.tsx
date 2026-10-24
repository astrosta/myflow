import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createSkill,
  listMySkills,
  listSkills,
  openDraft,
  SkillApiError,
  type SkillListItem,
} from "@/core/api/skills";

import { MySkillsPage } from "./my-skills-page";

/**
 * 「我的技能」页：私有草稿 + 公共目录。
 *
 * 这里测的是**页面装配**（查询、空态、错误、创建后跳转），所以 API 模块整体
 * 换成 vi.fn()，只断言"页面有没有按约定调用它、拿到数据之后画成什么样"。
 * 路由同理：next/navigation 在 vitest 里没有路由上下文，useRouter 换成记录
 * 参数的假对象，断言的是 push 的目标路径。
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
    openDraft: vi.fn(),
  };
});

// 页面上"自己的公共技能"才给 编辑：会话 id 固定成 u-alice，行上的 owner_user_id 与它比对。
vi.mock("@/core/auth/use-session", () => ({
  useSession: () => ({
    user: { id: "u-alice", email: "alice@example.com", username: "alice", system_role: "user" },
    status: "authenticated",
    loading: false,
    refresh: async () => {},
    signOut: async () => {},
  }),
}));

const mockedListMine = vi.mocked(listMySkills);
const mockedListCatalog = vi.mocked(listSkills);
const mockedCreate = vi.mocked(createSkill);
const mockedOpenDraft = vi.mocked(openDraft);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function makeSkill(overrides: Partial<SkillListItem>): SkillListItem {
  return {
    skill_id: "skill-1",
    id: "skill-1",
    name: "技能",
    description: "",
    scope: "private",
    owner_user_id: "u-alice",
    skill_version: null,
    version_id: null,
    git_commit: null,
    skill_path: null,
    status: "draft",
    enabled: true,
    global_enabled: null,
    container_file: null,
    ...overrides,
  };
}

/** 每个用例一份新的 QueryClient：缓存不跨用例，失败也不重试（错误态要立刻可见）。 */
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
  it("listMySkills 和 listSkills 都空时显示空态和创建入口", async () => {
    mockedListMine.mockResolvedValue([]);
    mockedListCatalog.mockResolvedValue([]);

    renderPage();

    expect(await screen.findByText("还没有技能")).toBeTruthy();
    expect(screen.getByRole("button", { name: "新建技能" })).toBeTruthy();
  });

  it("私有草稿行标出「私有」和状态「草稿」，并链到编辑器", async () => {
    mockedListMine.mockResolvedValue([
      makeSkill({ skill_id: "draft-1", name: "日报生成", scope: "private", status: "draft" }),
    ]);
    mockedListCatalog.mockResolvedValue([]);

    renderPage();

    const row = (await screen.findByText("日报生成")).closest("li");
    expect(row).toBeTruthy();
    expect(row?.textContent).toContain("私有");
    expect(row?.textContent).toContain("草稿");
    // 私有行本身就是进编辑器的入口
    expect(row?.querySelector("a")?.getAttribute("href")).toBe("/workspace/skills/draft-1");
  });

  it("目录行标出「公共」，自己的公共技能给一个「编辑」动作：先 openDraft 再跳编辑器", async () => {
    mockedListMine.mockResolvedValue([]);
    mockedListCatalog.mockResolvedValue([
      makeSkill({
        skill_id: "pub-1",
        name: "代码审查",
        scope: "public",
        status: "approved",
        owner_user_id: "u-alice",
      }),
    ]);
    mockedOpenDraft.mockResolvedValue([]);

    renderPage();

    const row = (await screen.findByText("代码审查")).closest("li");
    expect(row?.textContent).toContain("公共");
    expect(row?.textContent).toContain("已通过");

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));

    await waitFor(() => expect(mockedOpenDraft).toHaveBeenCalledWith("pub-1"));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/workspace/skills/pub-1"));
  });

  it("不是自己的公共技能不给「编辑」", async () => {
    mockedListMine.mockResolvedValue([]);
    mockedListCatalog.mockResolvedValue([
      makeSkill({ skill_id: "pub-2", name: "别人技能", scope: "public", owner_user_id: "u-bob" }),
    ]);

    renderPage();

    expect(await screen.findByText("别人技能")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "编辑" })).toBeNull();
  });

  it("创建对话框提交 name/description，成功后跳到新技能的编辑器", async () => {
    mockedListMine.mockResolvedValue([]);
    mockedListCatalog.mockResolvedValue([]);
    mockedCreate.mockResolvedValue(makeSkill({ skill_id: "new-1", name: "新技能" }));

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "新建技能" }));

    const dialog = screen.getByRole("dialog", { name: "新建技能" });
    expect(dialog).toBeTruthy();

    fireEvent.change(screen.getByLabelText("技能名称"), { target: { value: "周报助手" } });
    fireEvent.change(screen.getByLabelText("技能说明"), {
      target: { value: "把一周的记录整理成周报" },
    });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));

    await waitFor(() =>
      expect(mockedCreate).toHaveBeenCalledWith({
        name: "周报助手",
        description: "把一周的记录整理成周报",
      }),
    );
    await waitFor(() => expect(push).toHaveBeenCalledWith("/workspace/skills/new-1"));
  });

  it("接口报错时渲染一条人读的告警", async () => {
    mockedListMine.mockRejectedValue(
      new SkillApiError(500, "internal_error", "技能目录暂时不可用", null),
    );
    mockedListCatalog.mockRejectedValue(
      new SkillApiError(500, "internal_error", "技能目录暂时不可用", null),
    );

    renderPage();

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("技能目录暂时不可用");
    // 空态不能盖在错误上面：报错的时候画"还没有技能"是误导
    expect(screen.queryByText("还没有技能")).toBeNull();
  });
});
