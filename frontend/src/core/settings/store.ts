/**
 * 用户设置放 localStorage（M5 §2）：设置不需要跟服务端同步。
 */

export interface UserSettings {
  /** `config.yaml` 里 `models[].name` 之一；null = 用服务端默认 */
  modelName: string | null;
  /** 推理等级 id（`models[].reasoning_efforts[].id`）；null = 不指定，交给 provider */
  reasoningEffort: string | null;
  thinkingEnabled: boolean;
  /**
   * 是否由用户显式切换过。旧版本的 `thinkingEnabled: true` 只是默认值，
   * 不能被当成"用户打开了思考"——否则升级后仍然关不掉。
   */
  thinkingTouched: boolean;
}

export const DEFAULT_SETTINGS: UserSettings = {
  modelName: null,
  reasoningEffort: null,
  // Thinking is opt-in: a capable model is not a reason to spend reasoning
  // tokens on every turn. The composer exposes an explicit toggle.
  thinkingEnabled: false,
  thinkingTouched: false,
};

export const SETTINGS_STORAGE_KEY = "myflow.settings.v1";

export interface StorageLike {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function resolveStorage(storage?: StorageLike | null): StorageLike | null {
  if (storage !== undefined) return storage;
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    // 隐私模式下 getItem/setItem 会抛
    return null;
  }
}

export function parseSettings(raw: string | null): UserSettings {
  if (!raw) return { ...DEFAULT_SETTINGS };
  try {
    const parsed = JSON.parse(raw) as Partial<UserSettings>;
    return {
      modelName: typeof parsed.modelName === "string" ? parsed.modelName : null,
      reasoningEffort:
        typeof parsed.reasoningEffort === "string" ? parsed.reasoningEffort : null,
      // Old versions wrote the default true into storage as soon as the user
      // changed a model. Without the touched marker that true is not evidence
      // of an explicit choice, so treat it as "not configured" (off).
      thinkingEnabled:
        parsed.thinkingTouched === true && parsed.thinkingEnabled === true,
      thinkingTouched: parsed.thinkingTouched === true,
    };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

export function loadSettings(storage?: StorageLike | null): UserSettings {
  const resolved = resolveStorage(storage);
  if (!resolved) return { ...DEFAULT_SETTINGS };
  try {
    return parseSettings(resolved.getItem(SETTINGS_STORAGE_KEY));
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

export function saveSettings(
  patch: Partial<UserSettings>,
  storage?: StorageLike | null,
): UserSettings {
  const next = { ...loadSettings(storage), ...patch };
  if (patch.thinkingEnabled !== undefined) next.thinkingTouched = true;
  const resolved = resolveStorage(storage);
  try {
    resolved?.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(next));
  } catch {
    /* 配额或隐私模式：设置丢了不影响功能 */
  }
  return next;
}
