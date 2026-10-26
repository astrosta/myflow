import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getProfile, patchProfile } from "@/core/api/memory";

import { MyProfilePage } from "./my-profile-page";

vi.mock("@/core/api/memory", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/memory")>();
  return {
    ...actual,
    getProfile: vi.fn(),
    patchProfile: vi.fn(),
    clearProfileField: vi.fn(),
  };
});

const mockedGet = vi.mocked(getProfile);
const mockedPatch = vi.mocked(patchProfile);

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

describe("我的画像页", () => {
  it("展示画像证据，提交时带 expected_revision，且没有 pin", async () => {
    mockedGet.mockResolvedValue({
      schema_version: 1,
      revision: 4,
      fields: {
        "communication.language": {
          value: "zh-CN",
          source: "user_manual",
          evidence_tier: "EXPLICIT_MANDATE",
          confidence: 0.95,
          observed_at: "2026-10-26T00:00:00Z",
        },
      },
    });
    mockedPatch.mockResolvedValue({
      schema_version: 1,
      revision: 5,
      fields: {
        "communication.language": {
          value: "en-US",
          source: "user_manual",
          evidence_tier: "EXPLICIT_MANDATE",
          confidence: 0.95,
          observed_at: "2026-10-26T00:00:00Z",
        },
      },
    });

    renderPage();
    expect(await screen.findByText("zh-CN")).toBeTruthy();
    expect(document.body.textContent).toContain("EXPLICIT_MANDATE");
    expect(screen.queryByText("pinned")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.change(screen.getByLabelText("communication.language"), {
      target: { value: "en-US" },
    });
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(mockedPatch).toHaveBeenCalledWith(
        { "communication.language": "en-US" },
        4,
      ),
    );
  });
});
