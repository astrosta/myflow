import { ApiError } from "@/core/api/auth";

/**
 * `code` → 一句人话（M9 §2.12）。
 *
 * 三条规矩：
 *
 * 1. **按 `code` 判断，不按 `detail`**：detail 是给人看的英文措辞，随时可能变。
 * 2. 只有"服务端比前端更清楚细节"的那几个才直接展示 detail ——
 *    用户名是哪条规则没过、口令差几位、超了几个字节，前端算不出来。
 * 3. 每个失败都要给出**下一步**：去注册 / 找管理员 / 稍后重试。
 */

/** 服务端的 detail 里带着前端算不出来的细节，直接透传。 */
const SHOW_SERVER_DETAIL = new Set([
  "invalid_email",
  // 用户名的规则有四条（长度 / 字符集 / 首尾 / 保留字），只有服务端说得出是哪一条
  "invalid_username",
  "password_too_short",
  "password_too_long",
  "invalid_password",
]);

const TEXTS: Record<string, string> = {
  // 登录（`auth.local.specific_errors=true` 时后端会分得这么细）
  invalid_credentials: "用户名或密码不对",
  // 登录标识只有用户名；没注册时后端也只回显用户名
  account_not_found: "这个用户名还没有注册",
  // 旧版后端用的名字；留着是为了"前端先上线、后端后重启"的窗口期
  email_not_registered: "这个账号还没有注册",
  wrong_password: "密码不对，再试一次",
  account_disabled: "账号已被管理员禁用，请联系管理员恢复",
  account_deleted: "账号正在删除流程里，回收期内管理员可以恢复",
  login_rate_limited: "尝试次数太多，稍后再试",
  // 注册 / 初始化
  user_already_exists: "这个账号已经注册过了，直接登录即可",
  username_taken: "这个用户名被占用了，换一个",
  registration_closed: "这个部署没有开放自助注册，请联系管理员建号",
  admin_already_exists: "已经初始化过了，直接登录即可",
  // 会话
  unauthenticated: "登录状态已失效，请重新登录",
  // 服务端自己出了问题：**不要**说成"你没登录"，否则用户重新登录一百次还是同一个错
  auth_unavailable: "服务端读不出认证配置，请联系管理员（服务端日志里有详情）",
  session_required: "这个操作需要先登录",
  setup_required: "需要先设置新密码",
  // 其它
  origin_not_allowed: "请求来源不被允许，请从本站页面重试",
  csrf_failed: "页面令牌已失效，刷新后重试",
  permission_denied: "没有这个操作的权限",
  user_not_found: "账号不存在",
  cannot_modify_self: "不能对自己的账号做这个操作",
  last_admin: "至少要保留一个管理员",
  too_many_pats: "访问令牌数量已达上限，先吊销几个",
  invalid_scope: "令牌权限不合法",
  unknown_owner: "内部调用代表的账号不存在或已停用",
  channel_not_bound: "这个账号还没绑定该通道",
};

export function authErrorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? error.message : String(error);
  }
  const code = error.code ?? "";
  if (SHOW_SERVER_DETAIL.has(code)) return error.message;
  if (code === "login_rate_limited" && error.retryAfterSeconds) {
    return `${TEXTS.login_rate_limited}（${error.retryAfterSeconds} 秒后可再试）`;
  }
  // 认不出的 code：宁可显示服务端的原话，也不要吞掉信息换成一句笼统的失败
  return TEXTS[code] ?? error.message;
}

/**
 * 登录失败时要不要顺手给一个"去注册"的入口。
 *
 * 只在后端**明确说了"这个用户名没有账号"**时才给——`specific_errors=false` 的部署
 * 会把它收敛成 invalid_credentials，那时这里返回 false，UI 也就不会替用户
 * 断言"你没注册过"。
 */
export function suggestsRegistration(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  // 旧版后端的 email_not_registered 一起认：否则"前端已更新、后端还没重启"的窗口里
  // 用户会失去那个"去注册"的入口。
  return error.code === "account_not_found" || error.code === "email_not_registered";
}

/** 测试与 UI 共用：这个 code 有没有专门的文案。 */
export function hasAuthErrorText(code: string | null | undefined): boolean {
  return typeof code === "string" && code in TEXTS;
}
