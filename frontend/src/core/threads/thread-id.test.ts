import { afterEach, describe, expect, it, vi } from "vitest";

import { THREAD_ID_PATTERN } from "../api/threads";
import { generateThreadId } from "./thread-id";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("generateThreadId", () => {
  it("形状满足后端的 ThreadId 约束", () => {
    expect(generateThreadId()).toMatch(THREAD_ID_PATTERN);
  });

  it("每次都不一样", () => {
    expect(generateThreadId()).not.toBe(generateThreadId());
  });

  it("没有 randomUUID 时退回 getRandomValues，形状照样合法", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: (target: Uint8Array) => {
        for (let index = 0; index < target.length; index += 1) target[index] = index;
        return target;
      },
    });

    const id = generateThreadId();
    expect(id).toMatch(THREAD_ID_PATTERN);
    // 版本位是 4、变体位是 8/9/a/b：退化路径也要产出合法 UUIDv4
    expect(id[14]).toBe("4");
  });

  it("连 getRandomValues 都没有时也能出一个合法 id", () => {
    vi.stubGlobal("crypto", undefined);

    expect(generateThreadId()).toMatch(THREAD_ID_PATTERN);
  });
});
