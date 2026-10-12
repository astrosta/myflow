/**
 * 用户名的派生与校验。
 *
 * 规则**逐条对齐后端** `auth/models.py` 的 `normalize_username`，
 * 不然会出现"前端说没问题、后端 422"这种最难自查的不一致：
 *
 *   _USERNAME_MIN = 3, _USERNAME_MAX = 32
 *   _USERNAME_RE  = ^[a-z0-9](?:[a-z0-9_.-]{1,30}[a-z0-9])$
 *   RESERVED      = default / anonymous / auth-disabled / system / internal
 *
 * 大小写不敏感：`Alice` 会被后端接受并写成 `alice`。
 */

export const USERNAME_MIN = 3;
export const USERNAME_MAX = 32;

const USERNAME_RE = /^[a-z0-9](?:[a-z0-9_.-]{1,30}[a-z0-9])$/;

/** 这些名字在日志、桶名、运行期上下文里有特殊含义，后端不给注册 */
export const RESERVED_USERNAMES = new Set([
  "default",
  "anonymous",
  "auth-disabled",
  "system",
  "internal",
]);

export function normalizeUsername(raw: string): string {
  return raw.trim().toLowerCase();
}

/**
 * 从邮箱派生默认用户名：取 @ 前面那段（"邮箱名称"）。
 *
 * 只做**归一**（去空白、转小写），不做"修好它"的猜测——比如把非法字符替换掉。
 * 猜出来的名字和用户以为的不是一回事，而且可能恰好撞上别人的账号。
 * 形状不对就让 validateUsername 如实报错。
 */
export function usernameFromEmail(email: string): string {
  const at = email.indexOf("@");
  return normalizeUsername(at > 0 ? email.slice(0, at) : email);
}

/** 返回第一条不满足的规则；null 表示合法。文案和后端一样逐条说清。 */
export function validateUsername(raw: string): string | null {
  const value = normalizeUsername(raw);
  if (!value) return "用户名不能为空";
  if (value.length < USERNAME_MIN || value.length > USERNAME_MAX) {
    return "用户名需要 " + USERNAME_MIN + "–" + USERNAME_MAX + " 个字符（现在是 " + value.length + " 个）";
  }
  if (!USERNAME_RE.test(value)) {
    return "只能用字母、数字、下划线、点或连字符，并且必须以字母或数字开头和结尾";
  }
  if (RESERVED_USERNAMES.has(value)) return "「" + value + "」是保留名，换一个";
  return null;
}
