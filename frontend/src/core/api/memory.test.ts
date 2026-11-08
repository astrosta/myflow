import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./base", () => ({
  apiUrl: (path: string) => path,
  apiFetch: vi.fn(),
}));

import { apiFetch } from "./base";
import {
  getOnboarding,
  MemoryApiError,
  patchProfile,
  updateOnboarding,
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

  it("uses the onboarding endpoints", async () => {
    mockedFetch.mockResolvedValue(json({ status: "seen", should_show: true }));
    await getOnboarding();
    expect(mockedFetch.mock.calls[0]![0]).toBe("/memory/profile/onboarding");

    mockedFetch.mockResolvedValue(json({ status: "snoozed" }));
    await updateOnboarding("snooze");
    const [, onboardingInit] = mockedFetch.mock.calls[1]!;
    expect(onboardingInit?.method).toBe("POST");
    expect(JSON.parse(String(onboardingInit?.body))).toEqual({ action: "snooze" });
  });
});

void MemoryApiError;
