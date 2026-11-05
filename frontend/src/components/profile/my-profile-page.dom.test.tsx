import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  clearProfile,
  getOnboarding,
  getProfile,
  getProfileSchema,
  listProfileSuggestions,
  patchProfile,
  type ProfileFieldSpec,
  type ProfileView,
} from "@/core/api/memory";

import { MyProfilePage } from "./my-profile-page";

vi.mock("@/core/api/memory", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/memory")>();
  return {
    ...actual,
    getProfile: vi.fn(),
    getProfileSchema: vi.fn(),
    patchProfile: vi.fn(),
    clearProfile: vi.fn(),
    getOnboarding: vi.fn(),
    listProfileSuggestions: vi.fn(),
  };
});

const mockedGet = vi.mocked(getProfile);
const mockedSchema = vi.mocked(getProfileSchema);
const mockedPatch = vi.mocked(patchProfile);
const mockedClear = vi.mocked(clearProfile);
const mockedOnboarding = vi.mocked(getOnboarding);
const mockedSuggestions = vi.mocked(listProfileSuggestions);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

function spec(
  path: string,
  label: string,
  group: string,
  groupLabel: string,
  kind: ProfileFieldSpec["kind"],
  extra: Partial<ProfileFieldSpec> = {},
): ProfileFieldSpec {
  return {
    path,
    label,
    group,
    group_label: groupLabel,
    kind,
    description: label + "说明",
    placeholder: "",
    options: [],
    max_length: 256,
    max_items: 10,
    item_max_length: 64,
    explicit_only: false,
    ...extra,
  };
}

function makeSchema(): { schema_version: number; fields: ProfileFieldSpec[] } {
  return {
    schema_version: 1,
    fields: [
      spec("identity.preferred_name", "称呼", "identity", "身份", "text", {
        placeholder: "例如：小林",
        max_length: 64,
        explicit_only: true,
      }),
      spec("identity.locale", "地区", "identity", "身份", "locale", {
        placeholder: "例如：zh-CN",
      }),
      spec("communication.language", "语言偏好", "communication", "沟通", "language", {
        options: [
          { value: "zh-CN", label: "简体中文" },
          { value: "zh-TW", label: "繁体中文" },
          { value: "en", label: "English" },
          { value: "auto", label: "跟随当前对话语言" },
        ],
      }),
      spec("accessibility.needs", "无障碍需求", "accessibility", "无障碍", "string_list", {
        explicit_only: true,
        item_max_length: 120,
      }),
      spec("boundaries", "边界", "boundaries", "边界", "string_list", {
        explicit_only: true,
        item_max_length: 200,
      }),
      spec("communication.tone", "偏好语气", "communication", "沟通", "choice", {
        options: [
          { value: "neutral", label: "中性" },
          { value: "friendly", label: "友好" },
          { value: "formal", label: "正式" },
          { value: "direct", label: "直接" },
        ],
      }),
      spec("communication.verbosity", "详细程度", "communication", "沟通", "choice", {
        options: [
          { value: "concise", label: "简洁" },
          { value: "balanced", label: "均衡" },
          { value: "detailed", label: "详细" },
        ],
      }),
      spec("communication.format", "偏好格式", "communication", "沟通", "multi_choice", {
        options: [
          { value: "bullets", label: "要点" },
          { value: "tables", label: "表格" },
          { value: "steps", label: "分步" },
        ],
      }),
      spec("work.role", "工作角色", "work", "工作", "text", {
        placeholder: "例如：后端工程师",
        max_length: 100,
      }),
      spec("work.domains", "工作领域", "work", "工作", "string_list"),
      spec("work.tools", "常用工具", "work", "工作", "string_list", {
        max_items: 20,
      }),
      spec("work.expertise", "专业水平", "work", "工作", "choice", {
        options: [
          { value: "beginner", label: "入门" },
          { value: "intermediate", label: "进阶" },
          { value: "expert", label: "专家" },
        ],
      }),
    ],
  };
}

function stubOnboardingAndSuggestions() {
  mockedOnboarding.mockResolvedValue({
    status: "seen",
    should_show: false,
    profile_field_count: 0,
    snooze_until: null,
    dismissed_at: null,
    updated_at: "2026-11-02T00:00:00Z",
  });
  mockedSuggestions.mockResolvedValue({ items: [] });
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MyProfilePage />
    </QueryClientProvider>,
  );
}

const EMPTY_PROFILE: ProfileView = {
  schema_version: 1,
  revision: 1,
  etag: "etag-empty",
  updated_at: "2026-11-02T00:00:00Z",
  fields: {},
};

describe("我的画像：全字段表单", () => {
  it("画像为空时也渲染全部字段，保存只提交有变化的字段", async () => {
    mockedSchema.mockResolvedValue(makeSchema());
    mockedGet.mockResolvedValue(EMPTY_PROFILE);
    stubOnboardingAndSuggestions();
    mockedPatch.mockResolvedValue({
      ...EMPTY_PROFILE,
      revision: 2,
      etag: "etag-2",
      fields: {
        "identity.preferred_name": {
          value: "小林",
          source: "user_manual",
          evidence_tier: "USER_CONFIRMED",
          confidence: 1,
          observed_at: "2026-11-02T00:00:00Z",
        },
      },
    });

    renderPage();

    const name = await screen.findByLabelText("称呼");
    expect(name).toBeTruthy();
    expect((name as HTMLInputElement).value).toBe("");
    for (const label of [
      "地区",
      "语言偏好",
      "无障碍需求",
      "边界",
      "偏好语气",
      "详细程度",
      "工作角色",
      "工作领域",
      "常用工具",
      "专业水平",
    ]) {
      expect(screen.getByLabelText(label)).toBeTruthy();
    }
    // 多选字段用 fieldset/legend 分组；没有“还没有画像信息”的空状态
    expect(screen.getByRole("group", { name: "偏好格式" })).toBeTruthy();
    expect(screen.queryByText("还没有画像信息。")).toBeNull();

    fireEvent.change(name, { target: { value: " 小林 " } });
    fireEvent.change(screen.getByLabelText("语言偏好"), { target: { value: "en" } });
    fireEvent.change(screen.getByLabelText("偏好语气"), { target: { value: "direct" } });
    fireEvent.click(screen.getByLabelText("要点"));
    expect(screen.getByText("多个值用顿号分隔，最多 20 项")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("常用工具"), {
      target: { value: "FastAPI、Postgres" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() =>
      expect(mockedPatch).toHaveBeenCalledWith(
        {
          "identity.preferred_name": "小林",
          "communication.language": "en",
          "communication.tone": "direct",
          "communication.format": ["bullets"],
          "work.tools": ["FastAPI", "Postgres"],
        },
        1,
      ),
    );
  });

  it("预填已有值，清空字段保存为 null，撤销可以恢复", async () => {
    mockedSchema.mockResolvedValue(makeSchema());
    stubOnboardingAndSuggestions();
    mockedGet.mockResolvedValue({
      schema_version: 1,
      revision: 4,
      etag: "etag-4",
      updated_at: "2026-11-02T00:00:00Z",
      fields: {
        "identity.preferred_name": {
          value: "小王",
          source: "user_manual",
          evidence_tier: "USER_CONFIRMED",
          confidence: 1,
          observed_at: "2026-11-02T00:00:00Z",
        },
        "communication.language": {
          value: "zh-CN",
          source: "user_manual",
          evidence_tier: "USER_CONFIRMED",
          confidence: 1,
          observed_at: "2026-11-02T00:00:00Z",
        },
        "communication.format": {
          value: ["bullets", "steps"],
          source: "user_manual",
          evidence_tier: "USER_CONFIRMED",
          confidence: 1,
          observed_at: "2026-11-02T00:00:00Z",
        },
      },
    });
    mockedPatch.mockResolvedValue({
      schema_version: 1,
      revision: 5,
      etag: "etag-5",
      fields: {},
    });

    renderPage();

    const name = (await screen.findByLabelText("称呼")) as HTMLInputElement;
    await waitFor(() => expect(name.value).toBe("小王"));
    expect((screen.getByLabelText("语言偏好") as HTMLSelectElement).value).toBe("zh-CN");
    expect((screen.getByLabelText("要点") as HTMLInputElement).checked).toBe(true);
    expect((screen.getByLabelText("分步") as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText("没有未保存的修改。")).toBeTruthy();

    fireEvent.change(name, { target: { value: "" } });
    expect(screen.getByText("有未保存的修改；留空的字段保存后会被删除。")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "撤销修改" }));
    await waitFor(() =>
      expect((screen.getByLabelText("称呼") as HTMLInputElement).value).toBe("小王"),
    );

    fireEvent.change(screen.getByLabelText("称呼"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() =>
      expect(mockedPatch).toHaveBeenCalledWith({ "identity.preferred_name": null }, 4),
    );
  });

  it("清空画像需要确认", async () => {
    mockedSchema.mockResolvedValue(makeSchema());
    mockedGet.mockResolvedValue(EMPTY_PROFILE);
    stubOnboardingAndSuggestions();
    const confirm = vi.fn(() => false);
    vi.stubGlobal("confirm", confirm);
    mockedClear.mockResolvedValue(EMPTY_PROFILE);

    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "清空画像" }));
    expect(confirm).toHaveBeenCalled();
    expect(mockedClear).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    vi.stubGlobal("confirm", confirm);
    fireEvent.click(screen.getByRole("button", { name: "清空画像" }));
    await waitFor(() => expect(mockedClear).toHaveBeenCalledWith("etag-empty"));
  });
});
