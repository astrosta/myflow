import { describe, expect, it } from "vitest";

import { metadata } from "./page";

describe("/workspace/mcp-servers 路由", () => {
  it("浏览器标签叫「MCP 服务」", () => {
    expect(metadata.title).toBe("MCP 服务");
  });
});
