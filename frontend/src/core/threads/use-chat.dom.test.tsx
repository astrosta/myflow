import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

const { captured, streamMock } = vi.hoisted(() => {
  const streamMock = {
    messages: [],
    values: {},
    isLoading: false,
    isThreadLoading: false,
    error: null,
    submit: vi.fn(),
    stop: vi.fn(),
  };
  return { captured: [] as Record<string, unknown>[], streamMock };
});

vi.mock("@langchain/langgraph-sdk/react", () => ({
  useStream: (options: Record<string, unknown>) => {
    captured.push(options);
    return streamMock;
  },
}));
vi.mock("@/core/api/client", () => ({ getApiClient: () => ({}) }));
vi.mock("@/core/api/fetch-with-gap", () => ({
  WILDCARD_THREAD: "__any__",
  registerRecoveryContext: () => () => {},
}));
vi.mock("@/core/api/messages-feed", () => ({
  DEFAULT_PAGE_SIZE: 30,
  fetchMessagePage: () => new Promise(() => {}),
}));
vi.mock("@/core/api/threads", () => ({ getThreadState: vi.fn() }));
vi.mock("@/core/api/uploads", () => ({ uploadFiles: vi.fn() }));

import { useChat } from "./use-chat";

afterEach(cleanup);

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("useChat 的 thread 身份", () => {
  /**
   * 回归：`useStream` 的 `threadId` 是受控的。新会话首轮由 SDK 建出 thread 后，
   * 如果还继续传 prop（null），第二轮 submit 会再建一个 thread——后端按 thread
   * 派生 sandbox_id，前端折叠到旧 sandbox 时就会抛
   * `Reducer conflict on "sandbox"`。所以必须传解析后的 id。
   */
  it("首轮建出 thread 后，useStream 拿到的是这个 thread", () => {
    const { rerender } = renderHook(() => useChat({ threadId: null }), { wrapper });

    expect(captured.at(-1)?.threadId).toBeNull();

    act(() => {
      (captured.at(-1) as { onThreadId: (id: string) => void }).onThreadId("t1");
    });
    rerender();

    expect(captured.at(-1)?.threadId).toBe("t1");
    rerender();
    expect(captured.at(-1)?.threadId).toBe("t1");
  });
});
