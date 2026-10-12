import { describe, expect, it } from "vitest";

import { isAuthEndpoint } from "@/core/api/unauthorized";

describe("isAuthEndpoint", () => {
  it("认证端点上的 401/403 不广播成'会话失效'", () => {
    for (const url of [
      "/api/v1/auth/login/local",
      "/api/v1/auth/register",
      "/api/v1/auth/initialize",
      "/api/v1/auth/logout",
      "/api/v1/auth/setup-status",
      "/api/v1/auth/providers",
      "/api/v1/auth/change-password",
      "http://127.0.0.1:8001/api/v1/auth/login/local",
    ]) {
      expect(isAuthEndpoint(url), url).toBe(true);
    }
  });

  it("探活与业务路由照旧广播（那里 401 就是会话没了）", () => {
    for (const url of ["/api/v1/auth/me", "/api/threads", "/api/threads/t1/runs", "/api/models"]) {
      expect(isAuthEndpoint(url), url).toBe(false);
    }
  });
});
