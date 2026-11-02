import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  acceptProfileSuggestion,
  getOnboarding,
  getProfile,
  getProfileSchema,
  listProfileSuggestions,
  MemoryApiError,
  patchProfile,
  updateOnboarding,
  type ProfileFieldSpec,
} from "@/core/api/memory";

import { MyProfilePage } from "./my-profile-page";

vi.mock("@/core/api/memory", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/memory")>();
  return {
    ...actual,
    getProfile: vi.fn(),
    getProfileSchema: vi.fn(),
    getOnboarding: vi.fn(),
    listProfileSuggestions: vi.fn(),
    updateOnboarding: vi.fn(),
    acceptProfileSuggestion: vi.fn(),
    dismissProfileSuggestion: vi.fn(),
    patchProfile: vi.fn(),
    clearProfile: vi.fn(),
  };
});

const mockedProfile = vi.mocked(getProfile);
const mockedSchema = vi.mocked(getProfileSchema);
const mockedOnboarding = vi.mocked(getOnboarding);
const mockedSuggestions = vi.mocked(listProfileSuggestions);
const mockedUpdate = vi.mocked(updateOnboarding);
const mockedAccept = vi.mocked(acceptProfileSuggestion);
const mockedPatch = vi.mocked(patchProfile);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const SCHEMA_FIELD: ProfileFieldSpec = {
  path: "communication.verbosity",
  label: "详细程度",
  group: "communication",
  group_label: "沟通",
  kind: "choice",
  description: "回答的篇幅",
  placeholder: "",
  options: [
    { value: "concise", label: "简洁" },
    { value: "balanced", label: "均衡" },
    { value: "detailed", label: "详细" },
  ],
  max_length: 256,
  max_items: 10,
  item_max_length: 64,
  explicit_only: false,
};

const NAME_FIELD: ProfileFieldSpec = {
  ...SCHEMA_FIELD,
  path: "identity.preferred_name",
  label: "称呼",
  group: "identity",
  group_label: "身份",
  kind: "text",
  description: "希望被称呼的名字或昵称",
  placeholder: "例如：小林",
  options: [],
  max_length: 64,
  explicit_only: true,
};

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

describe("我的画像：引导、建议与错误", () => {
  it("展示引导和画像建议，并可稍后/接受", async () => {
    mockedSchema.mockResolvedValue({
      schema_version: 1,
      fields: [NAME_FIELD, SCHEMA_FIELD],
    });
    mockedProfile.mockResolvedValue({
      schema_version: 1,
      revision: 1,
      etag: "etag-1",
      updated_at: "2026-10-30T00:00:00Z",
      fields: {
        "identity.preferred_name": {
          value: "小林",
          source: "user_manual",
          evidence_tier: "USER_CONFIRMED",
          confidence: 1,
          observed_at: "2026-10-30T00:00:00Z",
        },
      },
    });
    mockedOnboarding.mockResolvedValue({
      status: "seen",
      should_show: true,
      profile_field_count: 1,
      snooze_until: null,
      dismissed_at: null,
      updated_at: "2026-10-30T00:00:00Z",
    });
    mockedSuggestions.mockResolvedValue({
      items: [
        {
          id: "s1",
          field_path: "communication.verbosity",
          value: "concise",
          evidence_uri: "viking://user/u/memories/preferences/style.md",
          evidence_quote: "喜欢简洁",
          confidence: 0.74,
          reason: "偏好简洁输出",
          status: "pending",
          profile_etag: "etag-1",
          expires_at: "2026-11-06T00:00:00Z",
        },
      ],
    });
    mockedUpdate.mockResolvedValue({
      status: "snoozed",
      should_show: false,
      profile_field_count: 1,
      snooze_until: "2026-11-06T00:00:00Z",
      dismissed_at: null,
      updated_at: "2026-10-30T00:00:00Z",
    });
    mockedAccept.mockResolvedValue({
      suggestion: { ...(await mockedSuggestions()).items[0]! },
      profile: { schema_version: 1, revision: 2, etag: "etag-2", fields: {} },
    });

    renderPage();
    expect(await screen.findByText("让助手更懂你")).toBeTruthy();
    // 建议值按 schema 的标签展示，而不是裸的内部枚举值
    const suggestionsPanel = await screen.findByRole("region", { name: "画像建议" });
    expect(within(suggestionsPanel).getByText("详细程度")).toBeTruthy();
    expect(within(suggestionsPanel).getByText("简洁")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "稍后" }));
    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledWith("snooze"));

    fireEvent.click(screen.getByRole("button", { name: "接受" }));
    await waitFor(() => expect(mockedAccept).toHaveBeenCalled());
  });

  it("保存冲突时展示可读错误并保留草稿", async () => {
    mockedSchema.mockResolvedValue({ schema_version: 1, fields: [NAME_FIELD] });
    mockedProfile.mockResolvedValue({
      schema_version: 1,
      revision: 3,
      etag: "etag-3",
      fields: {},
    });
    mockedOnboarding.mockResolvedValue({
      status: "dismissed",
      should_show: false,
      profile_field_count: 0,
      snooze_until: null,
      dismissed_at: "2026-10-30T00:00:00Z",
      updated_at: "2026-10-30T00:00:00Z",
    });
    mockedSuggestions.mockResolvedValue({ items: [] });
    mockedPatch.mockRejectedValue(new MemoryApiError(409, "profile_conflict", "stale etag"));

    renderPage();
    const name = await screen.findByLabelText("称呼");
    fireEvent.change(name, { target: { value: "小林" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    expect(
      await screen.findByText("画像已被其他操作更新，请刷新后再改。"),
    ).toBeTruthy();
    expect((screen.getByLabelText("称呼") as HTMLInputElement).value).toBe("小林");
  });
});
