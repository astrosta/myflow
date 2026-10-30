import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  acceptProfileSuggestion,
  getOnboarding,
  getProfile,
  listProfileSuggestions,
  updateOnboarding,
} from "@/core/api/memory";

import { MyProfilePage } from "./my-profile-page";

vi.mock("@/core/api/memory", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/memory")>();
  return {
    ...actual,
    getProfile: vi.fn(),
    getOnboarding: vi.fn(),
    listProfileSuggestions: vi.fn(),
    updateOnboarding: vi.fn(),
    acceptProfileSuggestion: vi.fn(),
    dismissProfileSuggestion: vi.fn(),
    patchProfile: vi.fn(),
    clearProfileField: vi.fn(),
  };
});

const mockedProfile = vi.mocked(getProfile);
const mockedOnboarding = vi.mocked(getOnboarding);
const mockedSuggestions = vi.mocked(listProfileSuggestions);
const mockedUpdate = vi.mocked(updateOnboarding);
const mockedAccept = vi.mocked(acceptProfileSuggestion);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

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

describe("我的画像：引导和建议", () => {
  it("展示引导和画像建议，并可稍后/接受", async () => {
    mockedProfile.mockResolvedValue({
      schema_version: 1,
      revision: 1,
      etag: "etag-1",
      updated_at: "2026-10-30T00:00:00Z",
      fields: {
        "communication.language": {
          value: "zh-CN",
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
      profile: {
        schema_version: 1,
        revision: 2,
        etag: "etag-2",
        fields: {},
      },
    });

    renderPage();
    expect(await screen.findByText("让助手更懂你")).toBeTruthy();
    expect(await screen.findByText("concise")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "稍后" }));
    await waitFor(() => expect(mockedUpdate).toHaveBeenCalledWith("snooze"));

    fireEvent.click(screen.getByRole("button", { name: "接受" }));
    await waitFor(() => expect(mockedAccept).toHaveBeenCalled());
  });
});
