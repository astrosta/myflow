import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { listPending, promoteSemantic } from "@/core/api/memory";

import { PendingMemoryPage } from "./pending-memory-page";

vi.mock("@/core/api/memory", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/core/api/memory")>();
  return {
    ...actual,
    listPending: vi.fn(),
    promoteSemantic: vi.fn(),
    deleteMemoryItem: vi.fn(),
  };
});

const mockedList = vi.mocked(listPending);
const mockedPromote = vi.mocked(promoteSemantic);

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
      <PendingMemoryPage />
    </QueryClientProvider>,
  );
}

describe("待确认记忆页", () => {
  it("展示证据，并用当前 revision 确认保留", async () => {
    mockedList.mockResolvedValue({
      items: [
        {
          id: "p1",
          content: "用户可能喜欢 unittest",
          confidence: 0.7,
          evidence_tier: "BEHAVIORAL_OBSERVED",
          evidence_quote: "我们有时用 unittest",
          source: "conversation",
          created_at: "2026-10-26T00:00:00Z",
          updated_at: "2026-10-26T00:00:00Z",
          revision: 3,
        },
      ],
      next_cursor: null,
    });
    mockedPromote.mockResolvedValue({ id: "p1" });

    renderPage();
    expect(await screen.findByText("用户可能喜欢 unittest")).toBeTruthy();
    expect(screen.getByText("我们有时用 unittest")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认保留" }));
    await waitFor(() => expect(mockedPromote).toHaveBeenCalledWith("p1", 3));
  });
});
