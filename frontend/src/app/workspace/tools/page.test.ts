import { describe, expect, it, vi } from "vitest";

import ToolsPage, { metadata } from "./page";

/**
 * 旧入口 /workspace/tools 必须继续可用：侧栏文案与地址都改了，但历史书签、文档里的
 * 链接、用户输入的 URL 还在。重定向放在服务端（渲染时 redirect），不是客户端 effect——
 * 否则会先闪一帧空页面。
 */

const { redirectSpy } = vi.hoisted(() => ({ redirectSpy: vi.fn() }));

vi.mock("next/navigation", () => ({ redirect: redirectSpy }));

describe("/workspace/tools 旧入口", () => {
  it("服务端重定向到 /workspace/mcp-servers", () => {
    ToolsPage();

    expect(redirectSpy).toHaveBeenCalledTimes(1);
    expect(redirectSpy).toHaveBeenCalledWith("/workspace/mcp-servers");
  });

  it("标题跟新页面一致，叫「MCP 服务」", () => {
    expect(metadata.title).toBe("MCP 服务");
  });
});
