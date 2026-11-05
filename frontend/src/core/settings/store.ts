/**
 * 用户设置放 localStorage（M5 §2）：设置不需要跟服务端同步。
 */

export interface UserSettings {
  /** `config.yaml` 里 `models[].name` 之一；null = 用服务端默认 */
  modelName: string | null;
  /** 思考等级 id（`models[].reasoning_efforts[].id`）；null = Off（思考关） */
  reasoningEffort: string | null;
}

export const DEFAULT_SETTINGS: UserSettings = {
  modelName: null,
  reasoningEffort: null,
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
  const resolved = resolveStorage(storage);
  try {
    resolved?.setItem(SETTINGS_STORAGE_KEY, JSON.stringify(next));
  } catch {
    /* 配额或隐私模式：设置丢了不影响功能 */
  }
  return next;
}
