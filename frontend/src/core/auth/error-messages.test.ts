import { describe, expect, it } from "vitest";

import { ApiError } from "@/core/api/auth";
import { authErrorMessage, hasAuthErrorText, suggestsRegistration } from "@/core/auth/error-messages";

function apiError(code: string | null, detail = "server detail", status = 401, retryAfter: number | null = null) {
  return new ApiError(status, code, detail, "trace-1", retryAfter);
}

describe("authErrorMessage", () => {
  it("把后端的 code 翻成人话，而不是把英文 detail 甩给用户", () => {
    expect(authErrorMessage(apiError("invalid_credentials"))).toBe("邮箱或密码不对");
    expect(authErrorMessage(apiError("account_not_found"))).toBe("这个账号还没有注册");
    // 旧版后端的名字仍然翻译（前端先上线、后端后重启的窗口期）
    expect(authErrorMessage(apiError("email_not_registered"))).toBe("这个账号还没有注册");
    expect(authErrorMessage(apiError("wrong_password"))).toBe("密码不对，再试一次");
    expect(authErrorMessage(apiError("user_already_exists"))).toBe("这个邮箱已经注册过了，直接登录即可");
    expect(authErrorMessage(apiError("username_taken", "…", 409))).toBe("这个用户名被占用了，换一个");
    expect(authErrorMessage(apiError("account_disabled", "…", 403))).toContain("管理员");
    expect(authErrorMessage(apiError("unauthenticated"))).toBe("登录状态已失效，请重新登录");
  });

  it("只有服务端才知道的细节直接透传 detail", () => {
    // 邮箱是哪条规则没过、口令差几位，前端算不出来
    expect(authErrorMessage(apiError("invalid_email", "email 'foo' must contain exactly one '@'", 422))).toBe(
      "email 'foo' must contain exactly one '@'",
    );
    expect(authErrorMessage(apiError("password_too_short", "password must be at least 8 characters (got 3)", 422))).toBe(
      "password must be at least 8 characters (got 3)",
    );
    // 用户名有四条规则，哪一条没过只有服务端知道
    expect(authErrorMessage(apiError("invalid_username", "username 'x' is reserved", 422))).toBe(
      "username 'x' is reserved",
    );
    expect(authErrorMessage(apiError("password_too_long", "… at most 72 bytes …", 422))).toContain("72 bytes");
  });

  it("429 用 Retry-After 说出还要等多久", () => {
    const limited = apiError("login_rate_limited", "too many failed login attempts", 429, 120);

    expect(authErrorMessage(limited)).toContain("120 秒");
    expect(authErrorMessage(apiError("login_rate_limited", "…", 429))).toBe("尝试次数太多，稍后再试");
  });

  it("不认识的 code 显示服务端原话，而不是吞成一句笼统的失败", () => {
    expect(authErrorMessage(apiError("brand_new_code", "something specific"))).toBe("something specific");
    expect(hasAuthErrorText("brand_new_code")).toBe(false);
    expect(hasAuthErrorText("wrong_password")).toBe(true);
  });

  it("非 ApiError 原样透出", () => {
    expect(authErrorMessage(new Error("boom"))).toBe("boom");
    expect(authErrorMessage("plain string")).toBe("plain string");
  });
});

describe("suggestsRegistration", () => {
  it("只有后端明确说'这个账号没注册'时才建议注册", () => {
    expect(suggestsRegistration(apiError("account_not_found"))).toBe(true);
    expect(suggestsRegistration(apiError("email_not_registered"))).toBe(true);
    // specific_errors=false 的部署只会给这个：不能替用户断言"你没注册过"
    expect(suggestsRegistration(apiError("invalid_credentials"))).toBe(false);
    expect(suggestsRegistration(new Error("boom"))).toBe(false);
  });
});
