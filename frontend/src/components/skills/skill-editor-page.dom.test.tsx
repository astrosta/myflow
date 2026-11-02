import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  deleteSkill,
  getDraft,
  listDraftFiles,
  readDraftFile,
  saveDraft,
  SkillApiError,
  submitSkill,
  uploadDraftFiles,
  withdrawSkill,
  writeDraftFile,
  type DraftFile,
  type DraftFileContent,
  type DraftView,
  type SkillVersion,
} from "@/core/api/skills";

import { SkillEditorPage } from "./skill-editor-page";

/**
 * 私有草稿编辑器：取数、本地编辑、保存/上传/提交/撤回/删除。
 *
 * API 模块整体换成 vi.fn()，只断言页面调没调对函数、参数对不对、拿到结果后
 * 画成什么样。保存顺序（先写文件再 saveDraft）用调用序号验证；本地编辑绝不
 * 自动保存，也是这里守住的。
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
    submitSkill: vi.fn(),
    withdrawSkill: vi.fn(),
    deleteSkill: vi.fn(),
  };
});

const mockedGetDraft = vi.mocked(getDraft);
const mockedListFiles = vi.mocked(listDraftFiles);
const mockedReadFile = vi.mocked(readDraftFile);
const mockedWriteFile = vi.mocked(writeDraftFile);
const mockedUpload = vi.mocked(uploadDraftFiles);
const mockedSave = vi.mocked(saveDraft);
const mockedSubmit = vi.mocked(submitSkill);
const mockedWithdraw = vi.mocked(withdrawSkill);
const mockedDelete = vi.mocked(deleteSkill);

const SKILL_ID = "skill-1";
const SKILL_MD = "# 日报生成\n\n把今天的记录整理成日报。";

function makeFile(path: string, overrides: Partial<DraftFile> = {}): DraftFile {
  return {
    path,
    size_bytes: 24,
    mode: "text",
    sha256: "a".repeat(64),
    ...overrides,
  };
}

function makeDraft(pending = false, status: "draft" | "pending" = pending ? "pending" : "draft"): DraftView {
  return {
    skill: {
      skill_id: SKILL_ID,
      id: SKILL_ID,
      name: "日报生成",
      description: "整理日报",
      scope: "private",
      owner_user_id: "u-alice",
      skill_version: null,
      version_id: "ver-1",
      git_commit: null,
      skill_path: null,
      status,
      enabled: true,
      global_enabled: null,
      container_file: null,
    },
    files: [makeFile("SKILL.md"), makeFile("scripts/run.py")],
    pending,
  };
}

function makeContent(path: string, content: string): DraftFileContent {
  return { ...makeFile(path), content, encoding: "utf-8" };
}

function makeVersion(): SkillVersion {
  return {
    version_id: "ver-2",
    skill_version: "0.1.0",
    package_sha256: "b".repeat(64),
    skill_md_sha256: "c".repeat(64),
    draft_commit: "commit-abc",
    index_status: "ready",
    created_at: "2026-10-23T00:00:00Z",
  };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SkillEditorPage skillId={SKILL_ID} />
    </QueryClientProvider>,
  );
}

/** 每个用例一致的初始响应；单个用例再覆盖需要的调用。 */
function primeDraft(pending = false) {
  mockedGetDraft.mockResolvedValue(makeDraft(pending));
  mockedListFiles.mockResolvedValue([makeFile("SKILL.md"), makeFile("scripts/run.py")]);
  mockedReadFile.mockImplementation(async (_skillId, path) =>
    makeContent(path, path === "SKILL.md" ? SKILL_MD : "print('hi')\n"),
  );
  mockedWriteFile.mockResolvedValue(makeFile("SKILL.md"));
  mockedSave.mockResolvedValue(makeVersion());
}

beforeEach(() => {
  primeDraft(false);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("私有技能编辑器", () => {
  it("加载草稿和文件树后，在文本框里显示 SKILL.md 内容", async () => {
    renderPage();

    const editor = (await screen.findByLabelText("文件内容")) as HTMLTextAreaElement;
    expect(editor.value).toBe(SKILL_MD);
    expect(screen.getByRole("button", { name: "SKILL.md" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /scripts\/run.py/ })).toBeTruthy();
    expect(screen.getByText("ver-1")).toBeTruthy();
  });

  it("编辑后点保存：先 writeDraftFile 再 saveDraft，并显示保存后的版本信息", async () => {
    renderPage();

    const editor = (await screen.findByLabelText("文件内容")) as HTMLTextAreaElement;
    fireEvent.change(editor, { target: { value: "# 新日报\n" } });
    // 未点保存之前不自动写
    expect(mockedWriteFile).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(mockedWriteFile).toHaveBeenCalledWith(SKILL_ID, {
        path: "SKILL.md",
        content: "# 新日报\n",
      }),
    );
    await waitFor(() => expect(mockedSave).toHaveBeenCalledWith(SKILL_ID));
    expect(mockedWriteFile.mock.invocationCallOrder[0]!).toBeLessThan(
      mockedSave.mock.invocationCallOrder[0]!,
    );
    expect(await screen.findByText("commit-abc")).toBeTruthy();
    expect(screen.getByText("ready")).toBeTruthy();
  });

  it("选择附件后调用 uploadDraftFiles（客户端内部用 FormData）", async () => {
    renderPage();

    await screen.findByLabelText("文件内容");
    const file = new File(["附件"], "notes.txt", { type: "text/plain" });
    fireEvent.change(screen.getByLabelText("上传附件"), { target: { files: [file] } });

    await waitFor(() => expect(mockedUpload).toHaveBeenCalledWith(SKILL_ID, [file]));
  });

  it("被驳回时显示驳回原因，并保持保存可用", async () => {
    const rejected = makeDraft(false);
    rejected.skill.status = "rejected";
    rejected.skill.reject_reason = "add a script example";
    mockedGetDraft.mockResolvedValue(rejected);

    renderPage();

    await screen.findByLabelText("文件内容");
    expect(screen.getByText(/add a script example/)).toBeTruthy();
    expect(screen.getAllByText("已驳回").length).toBeGreaterThan(0);
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("审核中时保存禁用、显示撤回提示和撤回按钮", async () => {
    primeDraft(true);
    mockedWithdraw.mockResolvedValue({
      submission_id: "sub-1",
      skill_id: SKILL_ID,
      version_id: "ver-2",
      status: "withdrawn",
      submitted_at: "2026-10-23T00:00:00Z",
      owner_user_id: "u-alice",
    });

    renderPage();

    await screen.findByLabelText("文件内容");
    expect(screen.getByText("审核中，请先撤回")).toBeTruthy();
    expect((screen.getByRole("button", { name: "保存" }) as HTMLButtonElement).disabled).toBe(true);

    fireEvent.click(screen.getByRole("button", { name: "撤回" }));
    await waitFor(() => expect(mockedWithdraw).toHaveBeenCalledWith(SKILL_ID));
  });

  it("未保存的草稿提交审核时提示先保存，不请求提交", async () => {
    const unsaved = makeDraft(false);
    unsaved.skill.version_id = null;
    mockedGetDraft.mockResolvedValue(unsaved);

    renderPage();

    await screen.findByLabelText("文件内容");
    fireEvent.click(screen.getByRole("button", { name: "提交审核" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("请先保存");
    expect(mockedSubmit).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
  });

  it("服务端返回 no saved version 时也渲染友好提示", async () => {
    mockedSubmit.mockRejectedValue(
      new SkillApiError(409, "conflict", "skill 'skill-1' has no saved version", null),
    );

    renderPage();

    await screen.findByLabelText("文件内容");
    fireEvent.click(screen.getByRole("button", { name: "提交审核" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("请先保存");
    expect(alert.textContent).not.toContain("no saved version");
  });

  it("提交成功后回到技能列表", async () => {
    mockedSubmit.mockResolvedValue({
      submission_id: "sub-1",
      skill_id: SKILL_ID,
      version_id: "ver-2",
      status: "pending",
      submitted_at: "2026-10-23T00:00:00Z",
      owner_user_id: "u-alice",
    });

    renderPage();

    await screen.findByLabelText("文件内容");
    fireEvent.click(screen.getByRole("button", { name: "提交审核" }));

    await waitFor(() => expect(mockedSubmit).toHaveBeenCalledWith(SKILL_ID));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/workspace/skills"));
  });

  it("提交遇到 409 时把冲突原因渲染成告警", async () => {
    mockedSubmit.mockRejectedValue(
      new SkillApiError(409, "conflict", "已有待审核的提交，请先撤回", "trace-1"),
    );

    renderPage();

    await screen.findByLabelText("文件内容");
    fireEvent.click(screen.getByRole("button", { name: "提交审核" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("已有待审核的提交，请先撤回");
  });

  it("审核中删除草稿时提示先撤回，不打开确认框也不请求删除", async () => {
    primeDraft(true);

    renderPage();

    await screen.findByLabelText("文件内容");
    fireEvent.click(screen.getByRole("button", { name: "删除草稿" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("请先撤回审核");
    expect(screen.queryByRole("dialog", { name: "删除草稿" })).toBeNull();
    expect(mockedDelete).not.toHaveBeenCalled();
  });

  it("删除草稿先弹确认对话框，确认后调用 deleteSkill 并回到列表", async () => {
    mockedDelete.mockResolvedValue(undefined);

    renderPage();

    await screen.findByLabelText("文件内容");
    fireEvent.click(screen.getByRole("button", { name: "删除草稿" }));

    expect(screen.getByRole("dialog", { name: "删除草稿" })).toBeTruthy();
    expect(mockedDelete).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));

    await waitFor(() => expect(mockedDelete).toHaveBeenCalledWith(SKILL_ID));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/workspace/skills"));
  });
});
