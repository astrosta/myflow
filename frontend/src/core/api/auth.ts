import { apiFetch, apiUrl } from "./base";

/**
 * 认证（M9 §2 的 9A）。
 *
 * 契约来自 §2.12 的路由表。三条要点：
 *
 * 1. **JWT 只在 HttpOnly cookie `access_token` 里，响应体里没有 token**
 *    字符串（§2.4）。所以前端没有任何"存 token"的逻辑——登录成功了就是
 *    成功了，之后的请求靠浏览器自动带 cookie。
 * 2. 错误体统一是 `{detail, code, trace_id}`。**`code` 才是给程序看的**，
 *    `detail` 是给人看的，不要拿它做判断（§2.12）。
 * 3. 这几个端点都在公开白名单上（§2.5），未登录也能打。
 */

export interface AuthUser {
  id: string;
  /** 旧账号可能保留邮箱；新注册账号为 null。 */
  email: string | null;
  /** 唯一登录标识，也是"给人看的名字"（§2.4）。改它不换桶。 */
  username: string;
  system_role: "admin" | "user";
}


export interface LoginResponse {
  expires_in: number;
  needs_setup: boolean;
}

/** 网关的错误信封 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly traceId: string | null;
  /** 429 上服务端给的 `Retry-After`（秒）。用来把"稍后再试"说成"120 秒后可再试"。 */
  readonly retryAfterSeconds: number | null;

  constructor(
    status: number,
    code: string | null,
    detail: string,
    traceId: string | null,
    retryAfterSeconds: number | null = null,
  ) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.traceId = traceId;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

async function readError(response: Response): Promise<ApiError> {
  let code: string | null = null;
  let detail = "HTTP " + response.status;
  let traceId: string | null = null;
  try {
    const body = (await response.json()) as Record<string, unknown>;
    if (typeof body.code === "string") code = body.code;
    if (typeof body.detail === "string") detail = body.detail;
    if (typeof body.trace_id === "string") traceId = body.trace_id;
  } catch {
    /* 非 JSON（502 之类的网关页），保持默认 detail */
  }
  const retryAfter = Number(response.headers.get("Retry-After"));
  return new ApiError(
    response.status,
    code,
    detail,
    traceId,
    Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
  );
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  const response = await apiFetch(apiUrl(path), { ...init, headers });
  if (!response.ok) throw await readError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function fetchProviders(): Promise<{ local: boolean }> {
  return request<{ local: boolean }>("/v1/auth/providers");
}

/**
 * 当前用户。**未登录返回 null 而不是抛异常**——每个页面都要问一次
 * "我是谁"，把 401 当异常会逼着每个调用点写 try/catch。
 */
/**
 * 会话探测的三种结果。
 *
 * 关键在于**把"未登录"和"认证根本没开"分开**——它们的 HTTP 状态码不同，
 * 而正确的反应完全相反（实测）：
 *
 *   401 unauthenticated   → 认证开着、没登录   → **该跳登录页**
 *   403 session_required  → 认证关着（§2.11 单人模式）→ **绝不能拦**，
 *                            业务请求全都正常，拦了就是把本地演示锁死
 *
 * 只看"拿不到 user 就跳登录"会把单人模式一起打掉。
 */
export type SessionProbe =
  | { status: "authenticated"; user: AuthUser }
  | { status: "anonymous" }
  | { status: "disabled" }
  | { status: "error"; message: string };

export async function probeSession(): Promise<SessionProbe> {
  try {
    return { status: "authenticated", user: await request<AuthUser>("/v1/auth/me") };
  } catch (error) {
    if (error instanceof ApiError) {
      if (error.status === 401) return { status: "anonymous" };
      if (error.status === 403 && error.code === "session_required") {
        return { status: "disabled" };
      }
    }
    // 网络/5xx：不当作"未登录"。把已登录的人踢去登录页比让他多等一会儿更糟
    return { status: "error", message: error instanceof Error ? error.message : String(error) };
  }
}

export async function fetchCurrentUser(): Promise<AuthUser | null> {
  try {
    return await request<AuthUser>("/v1/auth/me");
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) return null;
    throw error;
  }
}

/** 登录只认用户名 + 密码。 */
export function loginLocal(
  username: string,
  password: string,
  rememberMe = false,
): Promise<LoginResponse> {
  return request<LoginResponse>("/v1/auth/login/local", {
    method: "POST",
    body: JSON.stringify({ username, password, remember_me: rememberMe }),
  });
}

/** 首次初始化：只在库里还没有 admin 时可用，否则 409 admin_already_exists */
/** 自助注册；`auth.allow_registration` 为假时 403 registration_closed */
export function register(username: string, password: string): Promise<AuthUser> {
  return request<AuthUser>("/v1/auth/register", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function logout(): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("/v1/auth/logout", { method: "POST" });
}

/**
 * 改**自己**的用户名（`PATCH /v1/auth/me`，§2.12）。
 *
 * 三条前端要记住的约定：
 *
 * 1. **仅会话**：PAT 打这条是 403 `session_required`。
 * 2. **不动令牌**：改名不是撤销事件，当前会话与别的设备都继续有效。
 * 3. 响应体是完整的 `UserResponse`，所以调完直接拿它更新本地状态，
 *    不用再拉一次 `/me`（服务端才是权威，别自己拼一个假的）。
 */
export function updateUsername(username: string): Promise<AuthUser> {
  return request<AuthUser>("/v1/auth/me", {
    method: "PATCH",
    body: JSON.stringify({ username }),
  });
}

/**
 * 注销自己的账号。
 *
 * **后端目前没有这条路由，而且服务层明确禁止这么做**：
 *
 *   async def delete_user(self, *, actor_id, user_id):
 *       if actor_id == user_id:
 *           raise CannotModifySelf("cannot delete your own account")
 *
 * `delete_user` 是**管理员删别人**用的（§2.13 的用户生命周期），自助注销
 * 需要的是另一条路径：它得自己决定"回收期多长、期间能不能登录、桶什么时候真删"。
 * 这里按最自然的 REST 形状先接上，后端定了之后前端不用再动。
 */
export function deleteAccount(): Promise<void> {
  return request<void>("/v1/auth/me", { method: "DELETE" });
}

export function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<{ ok: boolean }> {
  return request<{ ok: boolean }>("/v1/auth/change-password", {
    method: "POST",
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
}
