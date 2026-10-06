import { existsSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { INCREMENTAL_STREAM_MODES, SUPPORTED_STREAM_MODES } from "./stream-mode";

/**
 * 前后端的 stream_mode 白名单必须一模一样。
 *
 * 两边漂移的症状是**一头静默、一头 422**：
 *   - 前端多认一个 → 请求发出去，网关 422，用户看到"莫名失败"
 *   - 前端少认一个 → 前端先抛错，后端其实支持，功能白白用不了
 *
 * 这种错误在 code review 里看不出来（两份列表在两个语言、两个目录里），
 * 所以让 CI 来对。
 */
const BACKEND_STREAM_MODES = fileURLToPath(
  new URL("../../../../backend/packages/harness/agentkit/runtime/stream_modes.py", import.meta.url),
);

function parseBackendWhitelist(source: string): string[] {
  const match = /RunStreamMode\s*=\s*Literal\[([^\]]+)\]/.exec(source);
  if (!match) throw new Error("could not find RunStreamMode Literal in stream_modes.py");
  return [...match[1]!.matchAll(/"([^"]+)"/g)].map((m) => m[1]!);
}

describe.skipIf(!existsSync(BACKEND_STREAM_MODES))("与后端 stream_mode 白名单的契约", () => {
  const source = readFileSync(BACKEND_STREAM_MODES, "utf8");
  const backend = parseBackendWhitelist(source);

  it("前端认的模式集合和后端完全一致", () => {
    expect([...SUPPORTED_STREAM_MODES].sort()).toEqual([...backend].sort());
  });

  it("增量模式三件套后端都支持", () => {
    for (const mode of INCREMENTAL_STREAM_MODES) {
      expect(backend).toContain(mode);
    }
  });

  it("后端的默认模式是 values —— 所以前端必须显式覆盖", () => {
    // normalize_stream_modes(None) → ["values"]
    expect(source).toContain('modes = ["values"]');
    // 这正是 §3.1 要在客户端包装层强制覆盖、而不是"不请求它"的原因
    expect(SUPPORTED_STREAM_MODES.has("values")).toBe(true);
    expect(INCREMENTAL_STREAM_MODES).not.toContain("values");
  });

  it("messages-tuple 在两侧都是公开名（内部名 messages 只在后端映射）", () => {
    expect(backend).toContain("messages-tuple");
    expect(backend).not.toContain("messages");
    expect(source).toContain('"messages" if mode == "messages-tuple"');
  });
});
