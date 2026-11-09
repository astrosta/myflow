import { describe, expect, it } from "vitest";

import { metadata } from "./page";

describe("/workspace/mcp-servers 路由", () => {
  it("浏览器标签叫「工具管理」", () => {
    expect(metadata.title).toBe("工具管理");
  });
});
