/**
 * CSRF double-submit（M9 §2.6）。
 *
 * 服务端设一个 **非 HttpOnly** 的 `csrf_token` cookie，浏览器在每个
 * state-changing 请求上带同值的 `X-CSRF-Token` 头，服务端用
 * `secrets.compare_digest` 比对。
 *
 * 需要 CSRF 的方法：POST / PUT / DELETE / PATCH。
 * 不需要：GET / HEAD / OPTIONS。
 */

export const CSRF_COOKIE_NAME = "csrf_token";
export const CSRF_HEADER_NAME = "X-CSRF-Token";

const STATE_CHANGING = new Set(["POST", "PUT", "DELETE", "PATCH"]);

/** cookie 是 document.cookie 里的一段；名字里有点号之类的要按字面匹配 */
export function readCookie(name: string, cookieString?: string): string | null {
  const source = cookieString ?? (typeof document === "undefined" ? "" : document.cookie);
  if (!source) return null;
  for (const part of source.split(";")) {
    const trimmed = part.trim();
    const eq = trimmed.indexOf("=");
    if (eq < 0) continue;
    if (trimmed.slice(0, eq) === name) {
      return decodeURIComponent(trimmed.slice(eq + 1));
    }
  }
  return null;
}

export function csrfToken(): string | null {
  return readCookie(CSRF_COOKIE_NAME);
}

export function methodNeedsCsrf(method: string | undefined): boolean {
  return STATE_CHANGING.has((method ?? "GET").toUpperCase());
}

/**
 * 给请求补上 CSRF 头。已经有（大小写任意）就不动。
 * 拿不到 token 时**不抛**——auth 关掉时本来就没有这个 cookie，
 * 真需要它的场合服务端会 403，比在客户端静默失败好定位。
 */
export function withCsrf(headers: Headers, method: string | undefined): Headers {
  if (!methodNeedsCsrf(method)) return headers;
  if (headers.has(CSRF_HEADER_NAME)) return headers;
  const token = csrfToken();
  if (token) headers.set(CSRF_HEADER_NAME, token);
  return headers;
}
