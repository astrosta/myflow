import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { confirmSkillCandidate, listSkillCandidates } from "@/core/api/memory";

import { SkillSuggestions } from "./skill-suggestions";

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), back: vi.fn(), refresh: vi.fn() }),
}));

vi.mock("@/core/api/memory", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/memory")>();
  return {
    ...actual,
    listSkillCandidates: vi.fn(),
    confirmSkillCandidate: vi.fn(),
    rejectSkillCandidate: vi.fn(),
  };
});

const mockedList = vi.mocked(listSkillCandidates);
const mockedConfirm = vi.mocked(confirmSkillCandidate);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderComponent() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <SkillSuggestions />
    </QueryClientProvider>,
  );
}

describe("记忆建议", () => {
  it("确认时带 revision/hash/idempotency，并打开现有技能编辑器", async () => {
    mockedList.mockResolvedValue([
      {
        id: "c1",
        status: "pending_confirmation",
        revision: 2,
        private_name: "deploy-fix",
        description: "修复部署",
        trigger_text: "部署失败",
        package_sha256: "a".repeat(64),
        source_episode_ids: ["e1", "e2", "e3"],
        created_at: "2026-10-26T00:00:00Z",
      },
    ]);
    mockedConfirm.mockResolvedValue({
      candidate_id: "c1",
      skill_id: "skill-1",
      version_id: "v1",
    });

    renderComponent();
    fireEvent.click(await screen.findByRole("button", { name: "创建私有技能" }));
    await waitFor(() =>
      expect(mockedConfirm).toHaveBeenCalledWith("c1", {
        expected_revision: 2,
        package_sha256: "a".repeat(64),
        idempotency_key: expect.any(String),
      }),
    );
    expect(push).toHaveBeenCalledWith("/workspace/skills/skill-1");
  });
});
