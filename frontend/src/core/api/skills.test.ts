import { afterEach, describe, expect, it, vi } from "vitest";

import {
  SkillApiError,
  approveReview,
  activateRelease,
  createSkill,
  deleteDraftFile,
  listMySkills,
  listSkills,
  openDraft,
  readDraftFile,
  uploadDraftFiles,
} from "./skills";

/**
 * 契约测试：只钉住**跨端真的会漂移**的东西（路径、方法、body、错误体）。
 * 不测 UI，也不测网关行为——那两边各自有测试。
 */

const SKILL = {
  id: "sk_1",
  name: "pdf-tools",
  description: "PDF 工具",
  scope: "private",
  owner_user_id: "u1",
  skill_version: null,
  version_id: null,
  git_commit: null,
  skill_path: null,
  status: "draft",
  enabled: true,
  global_enabled: null,
  container_file: "/mnt/skills/private/pdf-tools/SKILL.md",
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function stubFetch(response: Response): ReturnType<typeof vi.fn> {
  const mock = vi.fn(async () => response);
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("skills author API contract", () => {
  it("createSkill posts JSON through apiFetch and normalizes the wire id", async () => {
    const fetchMock = stubFetch(jsonResponse(SKILL, 201));

    const skill = await createSkill({ name: "pdf-tools", description: "PDF 工具" });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/skills");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    const headers = init.headers as Headers;
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(JSON.parse(String(init.body))).toEqual({ name: "pdf-tools", description: "PDF 工具" });
    // 作者路由的 wire 字段叫 id，计划里的类型叫 skill_id：客户端在边界上补齐。
    expect(skill.skill_id).toBe("sk_1");
  });

  it("openDraft posts to the draft/open route and returns the staged file list", async () => {
    const fetchMock = stubFetch(
      jsonResponse({
        files: [{ path: "SKILL.md", size_bytes: 12, mode: "text", sha256: "abc" }],
      }),
    );

    const files = await openDraft("sk 1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/skills/sk%201/draft/open");
    expect(init.method).toBe("POST");
    expect(files).toEqual([{ path: "SKILL.md", size_bytes: 12, mode: "text", sha256: "abc" }]);
  });

  it("uploadDraftFiles sends FormData and never sets Content-Type by hand", async () => {
    const fetchMock = stubFetch(
      jsonResponse({
        files: [{ path: "docs/a.md", size_bytes: 4, mode: "text", sha256: "def" }],
      }),
    );
    const file = new File(["data"], "docs/a.md", { type: "text/markdown" });

    const files = await uploadDraftFiles("sk_1", [file]);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/skills/sk_1/draft/files");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.headers as Headers).has("Content-Type")).toBe(false);
    expect(files.map((item) => item.path)).toEqual(["docs/a.md"]);
  });

  it("listMySkills maps the author wire id onto skill_id", async () => {
    const fetchMock = stubFetch(jsonResponse({ skills: [SKILL] }));

    const skills = await listMySkills();

    expect((fetchMock.mock.calls[0] as [string])[0]).toBe("/api/skills/mine");
    expect(skills[0]?.skill_id).toBe("sk_1");
  });

  it("listSkills('public') asks only for the published catalog", async () => {
    const fetchMock = stubFetch(jsonResponse({ skills: [SKILL] }));

    await listSkills("public");

    expect((fetchMock.mock.calls[0] as [string])[0]).toBe("/api/skills?scope=public");
  });

  it("listSkills maps a legacy filesystem category onto the catalog scope", async () => {
    stubFetch(
      jsonResponse({
        skills: [
          {
            name: "pdf-tools",
            description: "PDF 工具",
            category: "public",
            enabled: true,
          },
        ],
      }),
    );

    const [skill] = await listSkills();

    expect(skill?.scope).toBe("public");
  });

  it("readDraftFile encodes the path query parameter", async () => {
    const fetchMock = stubFetch(
      jsonResponse({
        path: "docs/a b.md",
        size_bytes: 4,
        mode: "text",
        sha256: "def",
        content: "data",
        encoding: "utf-8",
      }),
    );

    const file = await readDraftFile("sk_1", "docs/a b.md");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/skills/sk_1/draft/file?path=docs%2Fa%20b.md");
    expect(init.method ?? "GET").toBe("GET");
    expect(file.content).toBe("data");
    expect(file.encoding).toBe("utf-8");
  });

  it("deleteDraftFile accepts the 204 response without parsing JSON", async () => {
    const fetchMock = stubFetch(new Response(null, { status: 204 }));

    await expect(deleteDraftFile("sk_1", "docs/a.md")).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/skills/sk_1/draft/file?path=docs%2Fa.md");
    expect(init.method).toBe("DELETE");
  });
});

describe("skills admin review API contract", () => {
  it("approveReview posts to the admin review route", async () => {
    const fetchMock = stubFetch(
      jsonResponse(
        {
          release_id: "rel_1",
          skill_id: "sk_1",
          skill_version: "1.0.0",
          git_repository: "skills",
          git_commit: "c0ffee",
          skill_path: "skills/sk_1",
          public_name: "pdf-tools",
          source_commit: "deadbeef",
          approved_by: "admin",
          created_at: "2026-10-23T00:00:00Z",
        },
        201,
      ),
    );

    const release = await approveReview("sub 1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/admin/skills/reviews/sub%201/approve");
    expect(init.method).toBe("POST");
    expect(release.release_id).toBe("rel_1");
  });

  it("activateRelease posts the release id and accepts 204", async () => {
    const fetchMock = stubFetch(new Response(null, { status: 204 }));

    await expect(activateRelease("sk_1", "rel_1")).resolves.toBeUndefined();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/admin/skills/sk_1/activate");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ release_id: "rel_1" });
  });
});

describe("skills API errors", () => {
  it("surfaces the first field error instead of the generic validation detail", async () => {
    stubFetch(
      jsonResponse(
        {
          code: "invalid_request",
          detail: "Request validation failed",
          trace_id: "trace-422",
          errors: [
            {
              loc: ["body", "name"],
              msg: "String should match pattern '^[a-z0-9]+(?:-[a-z0-9]+)*$'",
              type: "string_pattern_mismatch",
            },
          ],
        },
        422,
      ),
    );

    const error = await createSkill({ name: "Weekly-Report", description: "周报" }).catch(
      (reason: unknown) => reason,
    );

    expect(error).toBeInstanceOf(SkillApiError);
    const apiError = error as SkillApiError;
    expect(apiError.status).toBe(422);
    expect(apiError.code).toBe("invalid_request");
    expect(apiError.message).toContain("name:");
    expect(apiError.message).toContain("String should match pattern");
    expect(apiError.message).not.toBe("Request validation failed");
  });

  it("turns a coded error body into SkillApiError", async () => {
    stubFetch(
      jsonResponse(
        { code: "skill_conflict", detail: "public name already taken", trace_id: "trace-9" },
        409,
      ),
    );

    const error = await createSkill({ name: "pdf-tools", description: "PDF 工具" }).catch(
      (reason: unknown) => reason,
    );

    expect(error).toBeInstanceOf(SkillApiError);
    const apiError = error as SkillApiError;
    expect(apiError.status).toBe(409);
    expect(apiError.code).toBe("skill_conflict");
    expect(apiError.detail).toBe("public name already taken");
    expect(apiError.traceId).toBe("trace-9");
    expect(apiError.message).toBe("public name already taken");
  });
});
