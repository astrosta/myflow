import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./base", () => ({
  apiUrl: (path: string) => path,
  apiFetch: vi.fn(),
}));

import { apiFetch } from "./base";
import {
  MemoryApiError,
  patchProfile,
  promoteSemantic,
} from "./memory";

const mockedFetch = vi.mocked(apiFetch);

afterEach(() => {
  vi.clearAllMocks();
});

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("memory API client", () => {
  it("sends expected_revision for profile patches", async () => {
    mockedFetch.mockResolvedValue(json({ revision: 2, fields: {} }));
    await patchProfile({ "communication.language": "en-US" }, 1);
    const [url, init] = mockedFetch.mock.calls[0]!;
    expect(url).toBe("/memory/profile");
    expect(init?.method).toBe("PATCH");
    expect(JSON.parse(String(init?.body))).toEqual({
      fields: { "communication.language": "en-US" },
      expected_revision: 1,
    });
  });

  it("maps a coded conflict to MemoryApiError", async () => {
    mockedFetch.mockResolvedValue(
      json({ code: "memory_conflict", detail: "stale" }, 409),
    );
    await expect(patchProfile({}, 0)).rejects.toMatchObject({
      name: "MemoryApiError",
      status: 409,
      code: "memory_conflict",
    });
  });

  it("promotes with the visible revision", async () => {
    mockedFetch.mockResolvedValue(json({ id: "p1" }));
    await promoteSemantic("p1", 3);
    const [, init] = mockedFetch.mock.calls[0]!;
    expect(JSON.parse(String(init?.body))).toEqual({ expected_revision: 3 });
  });
});

void MemoryApiError;
