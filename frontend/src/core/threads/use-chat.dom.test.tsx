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
    interrupts: [] as Array<{ id?: string; value?: unknown }>,
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

import { uploadFiles } from "@/core/api/uploads";
import { useChat } from "./use-chat";

const uploadFilesMock = vi.mocked(uploadFiles);

afterEach(cleanup);

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("useChat 的缓存失效", () => {
  /**
   * 回归：标题由 worker 在收尾时投影到 threads_meta.display_name。侧栏读的是
   * ["threads"] 这一份列表缓存；如果 run 结束后只失效历史消息，列表会继续用
   * 首轮开始前的旧数据，用户要刷新页面才看得到新标题。
   */
  it("run 结束后让侧栏重新拉取标题", () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
    });
    const invalidate = vi.spyOn(client, "invalidateQueries");
    renderHook(() => useChat({ threadId: "t1" }), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });

    act(() => {
      (captured.at(-1) as { onFinish: () => void }).onFinish();
    });

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["threads"] });
  });
});

describe("useChat 的图级中断", () => {
  it("允许时用 Command(resume) 续跑，而不是再发一条普通消息", async () => {
    streamMock.interrupts = [
      {
        id: "interrupt-1",
        value: {
          action_requests: [
            {
              name: "update_user_profile",
              args: { fields: { "identity.preferred_name": "小林" } },
              description: "确认更新画像",
            },
          ],
          review_configs: [{ allowed_decisions: ["approve", "reject"] }],
        },
      },
    ];
    streamMock.submit.mockClear();

    const { result } = renderHook(() => useChat({ threadId: "t1" }), { wrapper });
    expect(result.current.interrupt?.toolName).toBe("update_user_profile");

    await act(async () => {
      await result.current.respondToInterrupt("approve");
    });

    expect(streamMock.submit).toHaveBeenCalledWith(
      null,
      expect.objectContaining({
        command: { resume: { decisions: [{ type: "approve" }] } },
      }),
    );
    streamMock.interrupts = [];
  });
});

describe("useChat 的附件清单", () => {
  it("上传后把文件名和大小放进本轮 human message 的 additional_kwargs.files", async () => {
    streamMock.submit.mockClear();
    uploadFilesMock.mockReset();
    uploadFilesMock.mockResolvedValue([
      { path: "/mnt/user-data/uploads/note.md", filename: "note.md", size: 2 } as never,
    ]);
    const { result } = renderHook(() => useChat({ threadId: "t1" }), { wrapper });
    const file = new File(["hi"], "note.md", { type: "text/markdown" });

    await act(async () => {
      await result.current.submit("看这个", [file]);
    });

    expect(uploadFilesMock).toHaveBeenCalledWith("t1", [file]);
    const input = streamMock.submit.mock.calls.at(-1)?.[0] as {
      messages: Array<{ additional_kwargs?: { files?: unknown } }>;
    };
    expect(input.messages[0]!.additional_kwargs).toEqual({
      files: [{ filename: "note.md", size: 2 }],
    });
  });
});

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
