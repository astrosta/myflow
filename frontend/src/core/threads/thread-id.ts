/**
 * 客户端生成 thread id。
 *
 * 新会话带附件时必须由前端先定 id：上传端点挂在 thread 下
 * （`POST /threads/{id}/uploads` 会校验 thread 可见），而 SDK 只在自己第一次
 * submit 时才建 thread —— 晚于上传。先建再传，再把这个 id 交给
 * `stream.submit(..., { threadId })`，SDK 就复用同一个 thread 而不是另建一个。
 *
 * 形状必须满足后端 `ThreadId` 的约束 `^[A-Za-z0-9_-]{1,64}$`：
 * UUIDv4 的连字符形式正好 36 个字符。
 */
export function generateThreadId(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === "function") return cryptoApi.randomUUID();

  /**
   * 非安全上下文（http + 非 localhost）没有 `randomUUID`，退回
   * `getRandomValues`；两样都没有（老测试环境）就退 `Math.random` ——
   * 这里只需要"不重名"，不是密钥。
   */
  const bytes = new Uint8Array(16);
  if (typeof cryptoApi?.getRandomValues === "function") cryptoApi.getRandomValues(bytes);
  else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  // UUIDv4 的版本位与变体位，保证形状和 randomUUID 一致
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
