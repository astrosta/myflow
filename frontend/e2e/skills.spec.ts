import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * 技能目录的端到端验收（浏览器行为）。
 *
 * **一、不连真后端。** 和 chat.spec.ts 同一个原则：所有 /api 请求都在
 * page.route 里被手写 fixture 拦掉。技能这条链路比聊天简单——没有 SSE，
 * 所以全部用 route.fulfill 就够，不需要本地 mock gateway。
 *
 * **二、会话角色用 fixture 切换。** 「普通用户看不到审核入口」和
 * 「管理员能审」是同一套 UI 的两个分支，靠 /api/v1/auth/me 的
 * system_role 决定。这里不做真的登录，只换这一个响应——
 * 这和真实部署里"后端 admin scope 决定谁能审"并不冲突：前端只是据此画/不画入口。
 *
 * **三、用例末尾一律 expect(store.unhandled).toEqual([])。** 没 mock 到的
 * 端点显式 501，否则 UI 的问题会退化成"页面莫名其妙空白"。
 *
 * **跑法**：pnpm e2e（配置见 playwright.config.ts）。
 */

/* -------------------------------------------------------------------------- */
/* 可变的技能目录 fixture                                                      */
/* -------------------------------------------------------------------------- */

interface DraftFileFixture {
  path: string;
  size_bytes: number;
  mode: string;
  sha256: string;
}

interface Store {
  /** 当前会话的角色：决定 /auth/me 的返回 */
  role: "user" | "admin";
  /** 作者自己的私有草稿 */
  mine: { skill_id: string; name: string; status: string }[];
  /** 私有草稿里的文件 */
  files: DraftFileFixture[];
  /** 待审核提交（管理端队列） */
  reviews: {
    submission_id: string;
    skill_id: string;
    name: string;
    version: string;
    owner_user_id: string;
    submitted_at: string;
    submitted_commit: string;
  }[];
  /** 公共目录（只放已通过的版本） */
  publicSkills: { skill_id: string; name: string; skill_version: string }[];
  /** 没 mock 到的请求：用例末尾断言为空 */
  unhandled: string[];
}

const SKILL_ID = "skill-e2e-1";
const SUBMISSION_ID = "sub-e2e-1";
const COMMIT = "0123456789abcdef0123456789abcdef01234567";

function newStore(): Store {
  return { role: "user", mine: [], files: [], reviews: [], publicSkills: [], unhandled: [] };
}

const AUTH_USER = (role: "user" | "admin") => ({
  id: role === "admin" ? "u-admin" : "u-author",
  email: role + "@e2e.local",
  username: role,
  system_role: role,
});

function fileFixture(path: string, size: number): DraftFileFixture {
  return { path: path, size_bytes: size, mode: "text", sha256: "a".repeat(64) };
}

function draftView(store: Store): unknown {
  return {
    skill: {
      id: SKILL_ID,
      skill_id: SKILL_ID,
      name: "日报生成",
      description: "把今天的记录整理成日报",
      scope: "private",
      owner_user_id: "u-author",
      skill_version: null,
      version_id: "ver-1",
      git_commit: null,
      skill_path: null,
      status: store.reviews.length > 0 ? "pending" : "draft",
      enabled: true,
    },
    files: store.files,
    pending: store.reviews.length > 0,
  };
}

/* -------------------------------------------------------------------------- */
/* 端点 fixture                                                                */
/* -------------------------------------------------------------------------- */

function json(route: Route, status: number, body: unknown): Promise<void> {
  return route.fulfill({
    status: status,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function handleApi(route: Route, store: Store): Promise<void> {
  const request = route.request();
  const method = request.method();
  const url = new URL(request.url());
  const path = url.pathname;

  // ---- 工作区外壳进入时必发 ----
  if (path === "/api/v1/auth/me") return json(route, 200, AUTH_USER(store.role));
  if (path === "/api/v1/auth/providers") return json(route, 200, { local: true });
  if (path === "/api/info") return json(route, 200, {});
  if (path === "/api/models") return json(route, 200, { models: [] });
  if (path === "/api/threads" && method === "GET") {
    return json(route, 200, { data: [], next_cursor: null, has_more: false });
  }
  if (path === "/api/threads/search") {
    return json(route, 200, { data: [], next_cursor: null, has_more: false });
  }
  if (path === "/api/assistants" || path === "/api/assistants/search") {
    return json(route, 200, []);
  }

  // ---- 作者：新建 / 打开草稿 / 写文件 / 保存 / 提交 ----
  if (path === "/api/skills" && method === "POST") {
    store.mine = [
      { skill_id: SKILL_ID, name: "日报生成", status: "draft" },
      ...store.mine.filter((s) => s.skill_id !== SKILL_ID),
    ];
    store.files = [fileFixture("SKILL.md", 0)];
    return json(route, 200, { id: SKILL_ID, name: "日报生成", scope: "private", status: "draft" });
  }
  if (path === "/api/skills/mine") {
    return json(route, 200, {
      skills: store.mine.map((s) => ({
        id: s.skill_id,
        skill_id: s.skill_id,
        name: s.name,
        description: "把今天的记录整理成日报",
        scope: "private",
        owner_user_id: "u-author",
        status: s.status,
        enabled: true,
      })),
    });
  }
  if (path === "/api/skills") {
    // 公共目录：只有审核通过的那一条
    return json(route, 200, {
      skills: store.publicSkills.map((s) => ({
        skill_id: s.skill_id,
        name: s.name,
        description: "把今天的记录整理成日报",
        scope: "public",
        owner_user_id: "u-author",
        skill_version: s.skill_version,
        status: "approved",
        enabled: true,
      })),
    });
  }
  if (path === "/api/skills/" + SKILL_ID + "/draft/open" && method === "POST") {
    return json(route, 200, { files: store.files });
  }
  if (path === "/api/skills/" + SKILL_ID + "/draft/tree") {
    return json(route, 200, { files: store.files });
  }
  if (path === "/api/skills/" + SKILL_ID + "/draft" && method === "GET") {
    return json(route, 200, draftView(store));
  }
  if (path === "/api/skills/" + SKILL_ID + "/draft/file" && method === "GET") {
    const wanted = url.searchParams.get("path") ?? "SKILL.md";
    return json(route, 200, {
      ...fileFixture(wanted, 64),
      content: "# 日报生成\n\n把今天的记录整理成日报。",
      encoding: "utf-8",
    });
  }
  if (path === "/api/skills/" + SKILL_ID + "/draft/file" && method === "PUT") {
    const body = request.postDataJSON() as { path: string; content: string };
    store.files = [fileFixture(body.path, body.content.length)];
    return json(route, 200, fileFixture(body.path, body.content.length));
  }
  if (path === "/api/skills/" + SKILL_ID + "/draft/save" && method === "POST") {
    return json(route, 200, {
      version: {
        version_id: "ver-1",
        skill_version: "1.0.0",
        package_sha256: "b".repeat(64),
        skill_md_sha256: "c".repeat(64),
        draft_commit: COMMIT,
        index_status: "pending",
        created_at: "2026-10-20T08:00:00Z",
      },
    });
  }
  if (path === "/api/skills/" + SKILL_ID + "/submit" && method === "POST") {
    store.mine = store.mine.map((s) => ({ ...s, status: "pending" }));
    store.reviews = [
      {
        submission_id: SUBMISSION_ID,
        skill_id: SKILL_ID,
        name: "日报生成",
        version: "1.0.0",
        owner_user_id: "u-author",
        submitted_at: "2026-10-20T08:00:00Z",
        submitted_commit: COMMIT,
      },
    ];
    return json(route, 200, {
      submission_id: SUBMISSION_ID,
      skill_id: SKILL_ID,
      version_id: "ver-1",
      status: "pending",
      submitted_at: "2026-10-20T08:00:00Z",
      owner_user_id: "u-author",
    });
  }

  // ---- 管理端：队列 / 详情 / 通过 ----
  if (path === "/api/admin/skills/reviews" && method === "GET") {
    return json(route, 200, { items: store.reviews });
  }
  if (path === "/api/admin/skills/reviews/" + SUBMISSION_ID && method === "GET") {
    const summary = store.reviews[0];
    if (!summary) return json(route, 404, { code: "submission_not_found", detail: "没有这条提交" });
    return json(route, 200, {
      ...summary,
      files: store.files.length > 0 ? store.files : [fileFixture("SKILL.md", 64)],
      skill_md: "# 日报生成\n\n把今天的记录整理成日报。",
      diff: "+ 新增 SKILL.md",
      metadata: {},
      package_bytes: 2176,
    });
  }
  if (path === "/api/admin/skills/reviews/" + SUBMISSION_ID + "/approve" && method === "POST") {
    store.reviews = [];
    store.publicSkills = [{ skill_id: SKILL_ID, name: "日报生成", skill_version: "1.0.0" }];
    store.mine = store.mine.map((s) => ({ ...s, status: "approved" }));
    return json(route, 200, {
      release_id: "rel-e2e-1",
      skill_id: SKILL_ID,
      skill_version: "1.0.0",
      git_repository: "skills",
      git_commit: COMMIT,
      skill_path: "skills/daily",
      public_name: "daily-report",
      approved_by: "u-admin",
      created_at: "2026-10-20T09:00:00Z",
    });
  }

  // 漏拦截时显式失败，别让 UI 的问题变成"页面空白"
  store.unhandled.push(method + " " + path);
  return json(route, 501, { code: "unmocked_endpoint", detail: method + " " + path });
}

async function installSkillsApi(page: Page, store: Store): Promise<void> {
  await page.route("**/api/**", (route) => handleApi(route, store));
}

/* -------------------------------------------------------------------------- */
/* 用例                                                                        */
/* -------------------------------------------------------------------------- */

test("作者：新建草稿 → 写 SKILL.md → 保存 → 提交审核", async ({ page }) => {
  const store = newStore();
  store.role = "user";
  await installSkillsApi(page, store);

  await page.goto("/workspace/skills");
  await expect(page.getByRole("heading", { name: "技能" })).toBeVisible();

  // 新建草稿：只有名称和说明，正文进编辑器再写
  await page.getByRole("button", { name: "新建技能" }).click();
  await page.getByLabel("技能名称").fill("日报生成");
  await page.getByLabel("技能说明").fill("把今天的记录整理成日报");
  await page.getByRole("button", { name: "创建" }).click();

  // 建好之后直接进编辑器
  await expect(page).toHaveURL(new RegExp("/workspace/skills/" + SKILL_ID + "$"));
  const editor = page.getByLabel("文件内容");
  await expect(editor).toBeVisible();

  await editor.fill("# 日报生成\n\n把今天的记录整理成日报。");
  await page.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText("已保存")).toBeVisible();

  await page.getByRole("button", { name: "提交审核" }).click();
  await expect(page.getByText("已提交审核")).toBeVisible();

  // 提交之后作者这一行变成"审核中"，并且不能再改
  expect(store.reviews).toHaveLength(1);
  expect(store.unhandled).toEqual([]);
});

test("管理员：审核入口可见，通过之后这一条离开队列", async ({ page }) => {
  const store = newStore();
  store.role = "admin";
  store.mine = [{ skill_id: SKILL_ID, name: "日报生成", status: "pending" }];
  store.files = [fileFixture("SKILL.md", 64)];
  store.reviews = [
    {
      submission_id: SUBMISSION_ID,
      skill_id: SKILL_ID,
      name: "日报生成",
      version: "1.0.0",
      owner_user_id: "u-author",
      submitted_at: "2026-10-20T08:00:00Z",
      submitted_commit: COMMIT,
    },
  ];
  await installSkillsApi(page, store);

  await page.goto("/workspace/skills");
  // 侧栏对管理员多出「审核」入口
  const reviewLink = page.getByRole("link", { name: "审核" });
  await expect(reviewLink).toBeVisible();
  await reviewLink.click();
  await expect(page).toHaveURL(/\/workspace\/admin\/skills$/);

  await expect(page.getByText("待审核 1")).toBeVisible();
  await page.getByRole("link", { name: "查看详情" }).click();

  // 详情：作者、版本、提交 commit、SKILL.md、diff、包大小都在
  await expect(page.getByText("u-author")).toBeVisible();
  await expect(page.getByText("v1.0.0")).toBeVisible();
  await expect(page.getByText(COMMIT.slice(0, 12))).toBeVisible();
  await expect(page.getByText(/把今天的记录整理成日报/)).toBeVisible();
  await expect(page.getByText(/新增 SKILL\.md/)).toBeVisible();
  await expect(page.getByText(/2\.1 KB/)).toBeVisible();

  await page.getByRole("button", { name: "通过" }).click();
  await expect(page.getByText("没有待审核的提交")).toBeVisible();

  // 通过之后这一版进了公共目录
  expect(store.publicSkills.map((s) => s.skill_id)).toEqual([SKILL_ID]);
  expect(store.unhandled).toEqual([]);
});

test("另一会话：公共目录里能看到刚通过的技能", async ({ page }) => {
  const store = newStore();
  store.role = "user";
  // 另一个作者/读者的视角：他没有私有草稿，公共目录里只有已通过的那一条
  store.publicSkills = [{ skill_id: SKILL_ID, name: "日报生成", skill_version: "1.0.0" }];
  await installSkillsApi(page, store);

  await page.goto("/workspace/skills");

  const row = (await page.getByText("日报生成").first()).locator("xpath=ancestor::li[1]");
  await expect(row).toContainText("公共");
  await expect(row).toContainText("已通过");
  await expect(row).toContainText("v1.0.0");

  // 别人的公共技能不给「编辑」：编辑自己的需要先开一份私有草稿
  await expect(page.getByRole("button", { name: "编辑" })).toHaveCount(0);
  // 普通用户看不到审核入口
  await expect(page.getByRole("link", { name: "审核" })).toHaveCount(0);
  expect(store.unhandled).toEqual([]);
});
