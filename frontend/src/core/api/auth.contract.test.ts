import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, loginLocal, register, updateUsername } from "./auth";

/**
 * 前后端**协议**测试（M9 §2.12）。
 *
 * 后端那边 `test_auth_username_change.py` 从真路由钉同一件事；
 * 这里钉的是"前端发出去的那一发请求长什么样"——方法、路径、body 字段名，
 * 三个里错一个都是线上 404 / 422，而单看任一侧的代码都看不出来。
 */
const USER = {
  id: "u1",
  email: "alice@example.com",
  username: "alice",
  system_role: "user" as const,
};

function stubFetch(payload: unknown, status = 200) {
  const calls: { url: string; init: RequestInit }[] = [];
  const fetchMock = vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    return new Response(JSON.stringify(payload), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("username/password auth payloads", () => {
  it("登录只发 username + password", async () => {
    const calls = stubFetch({ expires_in: 60, needs_setup: false });

    await loginLocal("alice", "secret", true);

    const call = calls.at(0);
    if (!call) throw new Error("没有发出请求");
    expect(call.url).toBe("/api/v1/auth/login/local");
    expect(call.init.method).toBe("POST");
    expect(JSON.parse(String(call.init.body))).toEqual({
      username: "alice",
      password: "secret",
      remember_me: true,
    });
  });

  it("注册只发 username + password", async () => {
    const calls = stubFetch(USER);

    await register("alice", "secret");

    const call = calls.at(0);
    if (!call) throw new Error("没有发出请求");
    expect(call.url).toBe("/api/v1/auth/register");
    expect(call.init.method).toBe("POST");
    expect(JSON.parse(String(call.init.body))).toEqual({
      username: "alice",
      password: "secret",
    });
  });
});


describe("updateUsername", () => {
  it("发 PATCH /api/v1/auth/me，body 只有 username", async () => {
    const calls = stubFetch(USER);

    const user = await updateUsername("renamed");

    expect(user).toEqual(USER);
    expect(calls).toHaveLength(1);
    const call = calls.at(0);
    if (!call) throw new Error("没有发出请求");
    expect(call.url).toBe("/api/v1/auth/me");
    expect(call.init.method).toBe("PATCH");
    expect(JSON.parse(String(call.init.body))).toEqual({ username: "renamed" });
    expect(call.init.credentials).toBe("same-origin");
  });

  it("把后端的错误信封原样翻成 ApiError", async () => {
    stubFetch(
      { detail: "username 'x' is taken; pick another one", code: "username_taken", trace_id: "t1" },
      409,
    );

    const error = await updateUsername("x").catch((cause: unknown) => cause);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).code).toBe("username_taken");
    expect((error as ApiError).status).toBe(409);
  });
});
