/**
 * 会话到期时间。
 *
 * 登录响应里有 `expires_in`（M9 §2.12 的 LoginResponse），拿它算出到期时刻。
 * 有了这个就能**主动**跳登录页，而不是等某个请求 401 之后才被动反应——
 * 被动反应的表现是"界面先闪一下报错，然后才跳走"。
 *
 * 存 sessionStorage：刷新页面之后仍然知道什么时候过期；
 * 但**不放 localStorage**，那会让一个关掉的标签页的状态活到下次开浏览器。
 */

const STORAGE_KEY = "myflow.session.expires_at";

let expiresAt: number | null = null;

function storage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

/** 登录 / 注册 / 初始化成功之后调用 */
export function markSessionExpiry(expiresInSeconds: number): void {
  if (!Number.isFinite(expiresInSeconds) || expiresInSeconds <= 0) return;
  expiresAt = Date.now() + expiresInSeconds * 1000;
  try {
    storage()?.setItem(STORAGE_KEY, String(expiresAt));
  } catch {
    /* 隐私模式下写不进去；内存里那份仍然有效 */
  }
}

export function sessionExpiresAt(): number | null {
  if (expiresAt !== null) return expiresAt;
  const raw = storage()?.getItem(STORAGE_KEY);
  if (!raw) return null;
  const parsed = Number(raw);
  expiresAt = Number.isFinite(parsed) ? parsed : null;
  return expiresAt;
}

/** 退出登录时清掉 */
export function clearSessionExpiry(): void {
  expiresAt = null;
  try {
    storage()?.removeItem(STORAGE_KEY);
  } catch {
    /* 同上 */
  }
}

/** 已经过期（留 1 秒余量，避免刚好卡在边界上） */
export function isSessionExpired(now: number = Date.now()): boolean {
  const at = sessionExpiresAt();
  return at !== null && now >= at - 1000;
}

/**
 * "刚才还是登录着的"这个事实。
 *
 * 用来把 401 的 reason 推导出来，而不是让每个调用点自己传：
 *
 * - 之前有会话、现在被拒 → **expired**（用户看到"登录已过期"才对）
 * - 从来就没有会话 → **rejected**（用户看到的就是一个普通登录页）
 *
 * 不这么做的话，`register` / `initialize` 那条路径（响应里没有 expires_in，
 * 只能靠探活发现）会一律报成 rejected，登录页于是显示成干巴巴的
 * "用你的用户名和密码登录"，而不是"登录已过期"。
 */
const ACTIVE_KEY = "myflow.session.active";
let active: boolean | null = null;

export function markSessionActive(): void {
  active = true;
  try {
    storage()?.setItem(ACTIVE_KEY, "1");
  } catch {
    /* 同上 */
  }
}

export function wasSessionActive(): boolean {
  if (active !== null) return active;
  active = storage()?.getItem(ACTIVE_KEY) === "1";
  return active;
}

export function clearSessionActive(): void {
  active = false;
  try {
    storage()?.removeItem(ACTIVE_KEY);
  } catch {
    /* 同上 */
  }
}
