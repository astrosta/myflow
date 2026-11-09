import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  activateSkill,
  deleteSkill,
  getDraft,
  listDraftFiles,
  readDraftFile,
  saveDraft,
  scanSkill,
  SkillApiError,
  uploadDraftFiles,
  writeDraftFile,
  type DraftFile,
  type DraftFileContent,
  type DraftView,
  type SecurityScan,
  type SkillListItem,
  type SkillVersion,
} from "@/core/api/skills";

import { SkillEditorPage } from "./skill-editor-page";
import { clearSkillScanRun } from "./skill-scan-run";

const push = vi.fn();

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
    getDraft: vi.fn(),
    listDraftFiles: vi.fn(),
    readDraftFile: vi.fn(),
    writeDraftFile: vi.fn(),
    uploadDraftFiles: vi.fn(),
    saveDraft: vi.fn(),
    scanSkill: vi.fn(),
    activateSkill: vi.fn(),
    deleteSkill: vi.fn(),
  };
});

const mockedGetDraft = vi.mocked(getDraft);
const mockedListFiles = vi.mocked(listDraftFiles);
const mockedReadFile = vi.mocked(readDraftFile);
const mockedWriteFile = vi.mocked(writeDraftFile);
const mockedUpload = vi.mocked(uploadDraftFiles);
const mockedSave = vi.mocked(saveDraft);
const mockedScan = vi.mocked(scanSkill);
const mockedActivate = vi.mocked(activateSkill);
const mockedDelete = vi.mocked(deleteSkill);

const SKILL_ID = "skill-1";
const SKILL_MD = "# 日报生成\n\n把今天的记录整理成日报。";

function makeFile(path: string, overrides: Partial<DraftFile> = {}): DraftFile {
  return { path, size_bytes: 24, mode: "text", sha256: "a".repeat(64), ...overrides };
}

function makeScan(overrides: Partial<SecurityScan> = {}): SecurityScan {
  return {
    scan_id: "scan-1",
    skill_id: SKILL_ID,
    version_id: "ver-1",
    status: "completed",
    is_safe: true,
    max_severity: "SAFE",
    findings_count: 0,
    findings: [],
    scanner: "cisco-ai-skill-scanner",
    scanner_version: "2.3.2",
    duration_ms: 12,
    error: null,
    created_at: "2026-11-02T00:00:00Z",
    ...overrides,
  };
}

function makeSkill(overrides: Partial<SkillListItem> = {}): SkillListItem {
  return {
    skill_id: SKILL_ID,
    id: SKILL_ID,
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
      duration_ms: 12,
      created_at: "2026-11-02T00:00:00Z",
    },
    can_activate: true,
    activation_blocked_reason: null,
    ...overrides,
  };
}

function makeDraft(overrides: Partial<DraftView> = {}): DraftView {
  return {
    skill: makeSkill(),
    files: [makeFile("SKILL.md"), makeFile("scripts/run.py")],
    latest_version: makeVersion(),
    latest_scan: makeScan(),
    ...overrides,
  };
}

function makeVersion(): SkillVersion {
  return {
    version_id: "ver-1",
    skill_version: "v1",
    package_sha256: "p".repeat(64),
    skill_md_sha256: "s".repeat(64),
    draft_commit: "c".repeat(40),
    index_status: "unindexed",
    created_at: "2026-11-02T00:00:00Z",
  };
}

function makeContent(path: string, content: string): DraftFileContent {
  return { ...makeFile(path), content, encoding: "utf-8" };
}

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
}

function renderPage(client = makeClient()) {
  return render(
    <QueryClientProvider client={client}>
      <SkillEditorPage skillId={SKILL_ID} />
    </QueryClientProvider>,
  );
}

function primeHappyPath(draft = makeDraft()) {
  mockedGetDraft.mockResolvedValue(draft);
  mockedListFiles.mockResolvedValue(draft.files);
  mockedReadFile.mockResolvedValue(makeContent("SKILL.md", SKILL_MD));
  mockedWriteFile.mockResolvedValue(makeFile("SKILL.md"));
  mockedSave.mockResolvedValue(makeVersion());
  mockedScan.mockResolvedValue(makeScan());
  mockedActivate.mockResolvedValue({
    version: makeVersion(),
    active_version_id: "ver-1",
    active: true,
    index_status: "pending",
    indexed: false,
  });
  mockedDelete.mockResolvedValue(undefined);
  mockedUpload.mockResolvedValue(draft.files);
}

afterEach(() => {
  cleanup();
  clearSkillScanRun(SKILL_ID);
  vi.clearAllMocks();
});

describe("技能编辑器", () => {
  it("展示扫描结论与 finding，不渲染提交审核按钮", async () => {
    const scan = makeScan({
      is_safe: false,
      max_severity: "HIGH",
      findings_count: 1,
      findings: [
        {
          rule_id: "PROMPT_INJECTION",
          title: "可疑指令",
          severity: "HIGH",
          category: "prompt_injection",
          file_path: "SKILL.md",
          line_number: 7,
          remediation: "删除该指令",
        },
      ],
    });
    primeHappyPath(
      makeDraft({
        skill: makeSkill({
          status: "scan_failed",
          can_activate: false,
          activation_blocked_reason: "安全扫描未通过",
        }),
        latest_scan: scan,
      }),
    );

    renderPage();

    expect(await screen.findByText("可疑指令")).toBeTruthy();
    expect(screen.getByText(/最高 HIGH/)).toBeTruthy();
    expect(screen.getByText(/PROMPT_INJECTION/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "提交审核" })).toBeNull();
    expect(screen.queryByRole("button", { name: "撤回" })).toBeNull();
    expect((screen.getByRole("button", { name: "生效" }) as HTMLButtonElement).disabled).toBe(true);

    const textarea = screen.getByLabelText("文件内容");
    const scanPanel = screen.getByLabelText("安全扫描结果");
    expect(textarea.compareDocumentPosition(scanPanel) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("扫描发现按严重性排序，窗口固定 3 组、其余上下滑动", async () => {
    const findings = [
      { rule_id: "LOW_RULE", title: "低危提示", severity: "LOW" },
      { rule_id: "HIGH_RULE", title: "高危规则", severity: "HIGH" },
      { rule_id: "INFO_RULE", title: "信息项", severity: "INFO" },
      { rule_id: "CRIT_RULE", title: "严重规则", severity: "CRITICAL" },
      { rule_id: "MED_RULE", title: "中危规则", severity: "MEDIUM" },
      { rule_id: "SAFE_RULE", title: "安全项", severity: "SAFE" },
      { rule_id: "HIGH2_RULE", title: "另一个高危", severity: "HIGH" },
    ].map((finding) => ({
      ...finding,
      category: "prompt_injection",
      file_path: "SKILL.md",
      line_number: 7,
      remediation: null,
    }));
    primeHappyPath(
      makeDraft({
        latest_scan: makeScan({
          is_safe: false,
          max_severity: "CRITICAL",
          findings_count: findings.length,
          findings,
        }),
      }),
    );

    renderPage();

    const window = await screen.findByRole("group", { name: "扫描发现" });
    // 上下滑动：全部按严重性顺序渲染，窗口只决定"看得到几组"。
    const order = within(window)
      .getAllByRole("listitem")
      .map((item) => item.textContent ?? "");
    expect(order).toHaveLength(7);
    expect(order[0]).toContain("严重规则");
    expect(order[1]).toContain("另一个高危");
    expect(order[2]).toContain("高危规则");
    expect(order[3]).toContain("中危规则");
    expect(order[4]).toContain("低危提示");
    expect(order[5]).toContain("信息项");
    expect(order[6]).toContain("安全项");

    // 视口固定为 3 组高，超出的靠上下滚动；不再有翻页按钮。
    const viewport = within(window).getByRole("list", { name: "扫描发现列表" });
    expect(viewport.className).toContain("overflow-y-auto");
    expect(viewport.getAttribute("style") ?? "").toContain("calc(6rem * 3 + 0.5rem * 2)");
    expect(viewport.getAttribute("tabindex")).toBe("0"); // 键盘也能滚动
    expect(within(window).getByText(/共 7 组，上下滑动查看其余 4 组/)).toBeTruthy();
    expect(within(window).getByText("窗口 3 组")).toBeTruthy();
    expect(within(window).queryByRole("button", { name: "下一组发现" })).toBeNull();
    expect(within(window).queryByRole("button", { name: "上一组发现" })).toBeNull();
  });

  it("发现不多于 3 组时窗口不滚动也不抢焦点", async () => {
    primeHappyPath(
      makeDraft({
        latest_scan: makeScan({
          is_safe: false,
          max_severity: "HIGH",
          findings_count: 2,
          findings: [
            {
              rule_id: "HIGH_RULE",
              title: "高危规则",
              severity: "HIGH",
              category: "prompt_injection",
              file_path: "SKILL.md",
              line_number: 3,
              remediation: "删除该指令",
            },
            {
              rule_id: "LOW_RULE",
              title: "低危提示",
              severity: "LOW",
              category: "secrets",
              file_path: "scripts/run.sh",
              line_number: 9,
              remediation: null,
            },
          ],
        }),
      }),
    );

    renderPage();

    const window = await screen.findByRole("group", { name: "扫描发现" });
    expect(within(window).getAllByRole("listitem")).toHaveLength(2);
    expect(within(window).getByText(/共 2 组/)).toBeTruthy();
    expect(within(window).queryByText(/上下滑动/)).toBeNull();
    expect(
      (within(window).getByRole("list", { name: "扫描发现列表" }) as HTMLElement).getAttribute(
        "tabindex",
      ),
    ).toBe("-1");
  });

  it("只有存在未保存修改时保存才可用，保存后扫描才可用", async () => {
    primeHappyPath();
    mockedWriteFile.mockResolvedValue(makeFile("SKILL.md"));
    renderPage();

    const save = (await screen.findByRole("button", { name: "保存" })) as HTMLButtonElement;
    const scan = screen.getByRole("button", { name: "安全扫描" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(scan.disabled).toBe(false);

    const textarea = await screen.findByLabelText("文件内容");
    fireEvent.change(textarea, { target: { value: SKILL_MD + "\n新增" } });
    expect(save.disabled).toBe(false);
    expect(scan.disabled).toBe(true);

    fireEvent.click(save);

    await waitFor(() => expect(mockedWriteFile).toHaveBeenCalled());
    expect(mockedWriteFile.mock.calls[0]?.[1]).toMatchObject({
      path: "SKILL.md",
      content: SKILL_MD + "\n新增",
    });
    expect(mockedSave).toHaveBeenCalledWith(SKILL_ID);
    expect(mockedWriteFile.mock.invocationCallOrder[0]).toBeLessThan(
      mockedSave.mock.invocationCallOrder[0]!,
    );

    await waitFor(() => {
      expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(
        true,
      );
      expect(
        (screen.getByRole("button", { name: "安全扫描" }) as HTMLButtonElement).disabled,
      ).toBe(false);
    });
  });

  it("新技能没有已保存版本时保存按钮可用，名称和描述来自草稿", async () => {
    primeHappyPath(
      makeDraft({
        skill: makeSkill({
          name: "daily-report",
          description: "整理日报",
          status: "draft",
          skill_version: null,
          version_id: null,
          latest_scan: null,
          can_activate: false,
          activation_blocked_reason: "请先保存技能",
        }),
        latest_version: null,
        latest_scan: null,
      }),
    );
    renderPage();

    expect(await screen.findByRole("heading", { name: "daily-report" })).toBeTruthy();
    expect(await screen.findByText("整理日报")).toBeTruthy();
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(false);
    expect(
      (screen.getByRole("button", { name: "安全扫描" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("保存后界面实时刷新 SKILL.md 中的名称和描述", async () => {
    const before = makeDraft({
      skill: makeSkill({ name: "old-name", description: "旧描述" }),
    });
    const after = makeDraft({
      skill: makeSkill({ name: "new-name", description: "新描述" }),
    });
    primeHappyPath(before);
    mockedGetDraft.mockResolvedValueOnce(before).mockResolvedValue(after);
    renderPage();

    expect(await screen.findByRole("heading", { name: "old-name" })).toBeTruthy();
    const textarea = await screen.findByLabelText("文件内容");
    fireEvent.change(textarea, {
      target: {
        value: "---\nname: new-name\ndescription: 新描述\n---\n\n正文",
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    expect(await screen.findByRole("heading", { name: "new-name" })).toBeTruthy();
    expect(await screen.findByText("新描述")).toBeTruthy();
  });

  it("安全扫描调用 scanSkill 并刷新页面结果", async () => {
    primeHappyPath(makeDraft({ latest_scan: null, skill: makeSkill({ status: "unscanned" }) }));
    renderPage();

    expect(await screen.findByText(/尚未做安全扫描/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "安全扫描" }));

    await waitFor(() => expect(mockedScan.mock.calls[0]?.[0]).toBe(SKILL_ID));
    await waitFor(() => expect(screen.getByText(/安全扫描通过/)).toBeTruthy());
  });

  it("扫描过程中显示扫描器回传的状态", async () => {
    primeHappyPath(makeDraft({ latest_scan: null, skill: makeSkill({ status: "unscanned" }) }));
    let finish: (scan: ReturnType<typeof makeScan>) => void = () => {};
    mockedScan.mockImplementation((_skillId, onStatus) => {
      onStatus?.("正在做规则检查，并交给模型分析");
      return new Promise((resolve) => {
        finish = resolve;
      });
    });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "安全扫描" }));

    expect(await screen.findByText("正在做规则检查，并交给模型分析")).toBeTruthy();
    finish(makeScan());
    await waitFor(() => expect(screen.getByText(/安全扫描通过/)).toBeTruthy());
    expect(screen.queryByText("正在做规则检查，并交给模型分析")).toBeNull();
  });

  it("扫描中切到其他页面再回来仍保留状态，且不会重复发起扫描", async () => {
    const pending = makeDraft({
      latest_scan: null,
      skill: makeSkill({ status: "unscanned" }),
    });
    const scanned = makeDraft({
      latest_scan: makeScan(),
      skill: makeSkill({ status: "scanned" }),
    });
    primeHappyPath(pending);
    let complete = false;
    mockedGetDraft.mockImplementation(async () => (complete ? scanned : pending));

    let finish: (scan: ReturnType<typeof makeScan>) => void = () => {};
    mockedScan.mockImplementation((_skillId, onStatus) => {
      onStatus?.("正在扫描技能包");
      return new Promise((resolve) => {
        finish = resolve;
      });
    });

    const client = makeClient();
    const first = renderPage(client);
    fireEvent.click(await screen.findByRole("button", { name: "安全扫描" }));
    expect(await screen.findByText("正在扫描技能包")).toBeTruthy();

    // 模拟路由切走再回来：编辑器重新挂载，但扫描请求没有取消。
    first.unmount();
    renderPage(client);

    const scanningButton = (await screen.findByRole("button", {
      name: "扫描中…",
    })) as HTMLButtonElement;
    expect(scanningButton.disabled).toBe(true);
    expect(screen.getByText("正在扫描技能包")).toBeTruthy();
    expect(mockedScan).toHaveBeenCalledTimes(1);

    fireEvent.click(scanningButton);
    expect(mockedScan).toHaveBeenCalledTimes(1);

    complete = true;
    finish(makeScan());
    await waitFor(() => expect(screen.getByLabelText("安全扫描结果")).toBeTruthy());
    expect(screen.queryByText("正在扫描技能包")).toBeNull();
  });

  it("扫描失败后切到其他页面再回来仍显示失败原因", async () => {
    primeHappyPath(
      makeDraft({ latest_scan: null, skill: makeSkill({ status: "unscanned" }) }),
    );
    mockedScan.mockRejectedValue(
      new SkillApiError(503, "skill_scanner_unavailable", "扫描器暂不可用", null),
    );

    const client = makeClient();
    const first = renderPage(client);
    fireEvent.click(await screen.findByRole("button", { name: "安全扫描" }));
    expect(await screen.findByText("扫描器暂不可用")).toBeTruthy();

    first.unmount();
    renderPage(client);

    expect(await screen.findByText("扫描器暂不可用")).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "安全扫描" }) as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("有未保存修改时安全扫描按钮禁用，避免扫描到旧版本", async () => {
    primeHappyPath();
    renderPage();

    const textarea = await screen.findByLabelText("文件内容");
    const scan = screen.getByRole("button", { name: "安全扫描" }) as HTMLButtonElement;
    fireEvent.change(textarea, { target: { value: "changed" } });

    expect(scan.disabled).toBe(true);
    fireEvent.click(scan);
    expect(mockedScan).not.toHaveBeenCalled();
  });

  it("上传是一个入口：菜单里选文件夹后上传，仍要保存才能扫描", async () => {
    primeHappyPath();
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "上传" }));
    const menu = screen.getByRole("menu", { name: "上传方式" });
    const items = within(menu).getAllByRole("menuitem");
    expect(items.map((item) => item.textContent)).toEqual(["选择文件…", "选择文件夹…"]);

    const folderInput = screen.getByLabelText("选择文件夹") as HTMLInputElement;
    expect(folderInput.hasAttribute("webkitdirectory")).toBe(true);

    fireEvent.click(within(menu).getByRole("menuitem", { name: "选择文件夹…" }));
    expect(screen.queryByRole("menu", { name: "上传方式" })).toBeNull();

    const file = new File(["body"], "a.md", { type: "text/markdown" });
    Object.defineProperty(file, "webkitRelativePath", { value: "my-skill/docs/a.md" });
    Object.defineProperty(folderInput, "files", { configurable: true, value: [file] });
    fireEvent.change(folderInput);

    await waitFor(() => expect(mockedUpload).toHaveBeenCalledWith(SKILL_ID, [file]));
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(false);
    expect(
      (screen.getByRole("button", { name: "安全扫描" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("上传菜单支持 Esc 关闭，文件项驱动普通多选 input", async () => {
    primeHappyPath();
    renderPage();

    const trigger = await screen.findByRole("button", { name: "上传" });
    fireEvent.click(trigger);
    expect(within(screen.getByRole("menu", { name: "上传方式" })).getAllByRole("menuitem")).toHaveLength(2);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu", { name: "上传方式" })).toBeNull();

    const fileInput = screen.getByLabelText("选择文件") as HTMLInputElement;
    expect(fileInput.multiple).toBe(true);
    expect(fileInput.hasAttribute("webkitdirectory")).toBe(false);
  });

  it("点击生效先确认，确认成功后返回技能列表", async () => {
    primeHappyPath();
    renderPage();

    expect(await screen.findByLabelText("安全扫描结果")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "生效" }));

    const dialog = screen.getByRole("dialog", { name: "确认生效" });
    expect(mockedActivate).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认生效" }));

    await waitFor(() => expect(mockedActivate).toHaveBeenCalledWith(SKILL_ID, "ver-1"));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/workspace/skills"));
  });

  it("扫描未通过时生效按钮禁用", async () => {
    primeHappyPath(
      makeDraft({
        skill: makeSkill({
          status: "scan_failed",
          can_activate: false,
          activation_blocked_reason: "安全扫描未通过，请修复发现的问题后重新扫描",
        }),
        latest_scan: makeScan({ is_safe: false, max_severity: "CRITICAL", findings_count: 1 }),
      }),
    );
    renderPage();

    const button = (await screen.findByRole("button", { name: "生效" })) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText(/安全扫描未通过/)).toBeTruthy();
  });

  it("删除技能确认后跳回列表", async () => {
    primeHappyPath();
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "删除技能" }));
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(mockedDelete).toHaveBeenCalledWith(SKILL_ID));
    expect(push).toHaveBeenCalledWith("/workspace/skills");
  });
});
