import { describe, expect, it, vi } from "vitest";

import { fetchLatestRun } from "./threads";

describe("fetchLatestRun", () => {
  it("读取最新一条 run，保留后端错误文本", async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              run_id: "run-1",
              thread_id: "t1",
              status: "error",
              error: "GraphRecursionError: 模型在 1000 步内没有收敛",
              created_at: null,
              updated_at: null,
            },
          ],
          next_cursor: null,
          has_more: false,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    ) as unknown as typeof fetch;

    const run = await fetchLatestRun("t1", { fetchImpl });

    expect(fetchImpl).toHaveBeenCalledWith(
      "/api/threads/t1/runs?limit=1&order=desc",
      { signal: undefined },
    );
    expect(run?.status).toBe("error");
    expect(run?.error).toContain("GraphRecursionError");
  });

  it("没有 run 时返回 null", async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(JSON.stringify({ data: [], next_cursor: null, has_more: false }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ) as unknown as typeof fetch;

    await expect(fetchLatestRun("t1", { fetchImpl })).resolves.toBeNull();
  });
});
