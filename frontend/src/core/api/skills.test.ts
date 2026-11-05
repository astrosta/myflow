import { afterEach, describe, expect, it, vi } from "vitest";

import {
  SkillApiError,
  activateSkill,
  createSkill,
  deleteDraftFile,
  getDraft,
  listMySkills,
  listScans,
  listSkills,
  openDraft,
  readDraftFile,
  scanSkill,
  setSkillPreference,
  uploadDraftFiles,
} from "./skills";

/**
 * 契约测试：只钉住跨端真的会漂移的东西（路径、方法、body、错误体）。
 * 不测 UI，也不测网关行为——那两边各自有测试。
 */

const SKILL = {
  id: "sk_1",
  name: "pdf-tools",
  description: "PDF 工具",
  scope: "private",
  owner_user_id: "u1",
  skill_version: "v1",
  version_id: "version-1",
  git_commit: "abc",
  skill_path: "users/u1/sk_1/SKILL.md",
  status: "scanned",
  enabled: false,
  container_file: "/mnt/skills/private/pdf-tools/SKILL.md",
  index_status: "unindexed",
  active_version_id: null,
  active: false,
  latest_scan: {
    scan_id: "scan-1",
    version_id: "version-1",
    status: "completed",
    is_safe: true,
    max_severity: "SAFE",
    findings_count: 0,
    scanner_version: "2.3.2",
    duration_ms: 12,
    created_at: "2026-11-02T00:00:00Z",
  },
  can_activate: true,
  activation_blocked_reason: null,
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
    expect(skill.skill_id).toBe("sk_1");
    expect(skill.status).toBe("scanned");
    expect(skill.latest_scan?.is_safe).toBe(true);
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

  it("getDraft maps the skill row and scan payload", async () => {
    const fetchMock = stubFetch(
      jsonResponse({
        skill: SKILL,
        files: [],
        latest_version: { version_id: "version-1", skill_version: "v1", index_status: "unindexed" },
        latest_scan: {
          scan_id: "scan-1",
          skill_id: "sk_1",
          version_id: "version-1",
          status: "completed",
          is_safe: true,
          max_severity: "SAFE",
          findings_count: 0,
          findings: [],
          scanner: "cisco-ai-skill-scanner",
          scanner_version: "2.3.2",
          duration_ms: 12,
          error: null,
          created_at: "2026-11-02T00:00:00Z",
        },
      }),
    );

    const draft = await getDraft("sk_1");

    expect((fetchMock.mock.calls[0] as [string])[0]).toBe("/api/skills/sk_1/draft");
    expect(draft.skill.skill_id).toBe("sk_1");
    expect(draft.latest_version?.version_id).toBe("version-1");
    expect(draft.latest_scan?.scanner).toBe("cisco-ai-skill-scanner");
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

  it("uploadDraftFiles keeps folder structure while stripping the selected root", async () => {
    const fetchMock = stubFetch(
      jsonResponse({
        files: [{ path: "SKILL.md", size_bytes: 4, mode: "text", sha256: "def" }],
      }),
    );
    const skillFile = new File(["data"], "SKILL.md", { type: "text/markdown" });
    const script = new File(["print()"], "run.py", { type: "text/x-python" });
    Object.defineProperty(skillFile, "webkitRelativePath", { value: "my-skill/SKILL.md" });
    Object.defineProperty(script, "webkitRelativePath", {
      value: "my-skill/scripts/run.py",
    });

    await uploadDraftFiles("sk_1", [skillFile, script]);

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const uploaded = (init.body as FormData).getAll("files") as File[];
    expect(uploaded.map((file) => file.name)).toEqual(["SKILL.md", "scripts/run.py"]);
  });

  it("listMySkills maps the author wire id onto skill_id", async () => {
    const fetchMock = stubFetch(jsonResponse({ skills: [SKILL] }));

    const skills = await listMySkills();

    expect((fetchMock.mock.calls[0] as [string])[0]).toBe("/api/skills/mine");
    expect(skills[0]?.skill_id).toBe("sk_1");
    expect(skills[0]?.active).toBe(false);
    expect(skills[0]?.can_activate).toBe(true);
  });

  it("listSkills asks for builtins plus the caller's active skills", async () => {
    const fetchMock = stubFetch(jsonResponse({ skills: [SKILL] }));

    const [skill] = await listSkills();

    expect((fetchMock.mock.calls[0] as [string])[0]).toBe("/api/skills");
    expect(skill?.scope).toBe("private");
  });

  it("scanSkill posts to the scan route and returns persisted findings", async () => {
    const fetchMock = stubFetch(
      jsonResponse({
        scan_id: "scan-2",
        skill_id: "sk_1",
        version_id: "version-1",
        status: "completed",
        is_safe: false,
        max_severity: "HIGH",
        findings_count: 1,
        findings: [
          {
            rule_id: "PROMPT_INJECTION",
            title: "Prompt injection",
            severity: "HIGH",
            category: "prompt_injection",
            file_path: "SKILL.md",
            line_number: 7,
            remediation: "Remove the instruction.",
          },
        ],
        scanner: "cisco-ai-skill-scanner",
        scanner_version: "2.3.2",
        duration_ms: 20,
        error: null,
        created_at: "2026-11-02T00:00:00Z",
      }),
    );

    const scan = await scanSkill("sk_1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/skills/sk_1/scan");
    expect(init.method).toBe("POST");
    expect(scan.is_safe).toBe(false);
    expect(scan.findings[0]?.line_number).toBe(7);
  });

  it("scanSkill reads status events before the result frame", async () => {
    const statuses: string[] = [];
    const payload = [
      'event: status\ndata: {"message":"正在准备扫描器"}\n\n',
      'event: status\ndata: {"message":"正在做规则检查，并交给模型分析"}\n\n',
      'event: result\ndata: {"scan_id":"scan-2","skill_id":"sk_1","version_id":"version-1","status":"completed","is_safe":true,"max_severity":"SAFE","findings_count":0,"findings":[],"scanner":"cisco-ai-skill-scanner","scanner_version":"2.3.2","duration_ms":20,"error":null,"created_at":"2026-11-02T00:00:00Z"}\n\n',
    ].join("");
    const fetchMock = stubFetch(
      new Response(payload, { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    );

    const scan = await scanSkill("sk_1", (message) => statuses.push(message));

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Accept")).toBe("text/event-stream");
    expect(statuses).toEqual(["正在准备扫描器", "正在做规则检查，并交给模型分析"]);
    expect(scan.scan_id).toBe("scan-2");
    expect(scan.is_safe).toBe(true);
  });

  it("listScans encodes the limit query parameter", async () => {
    const fetchMock = stubFetch(jsonResponse({ scans: [] }));

    await listScans("sk_1", 5);

    expect((fetchMock.mock.calls[0] as [string])[0]).toBe("/api/skills/sk_1/scans?limit=5");
  });

  it("activateSkill posts the latest version and returns index status", async () => {
    const fetchMock = stubFetch(
      jsonResponse({
        version: { version_id: "version-1", skill_version: "v1", index_status: "pending" },
        active_version_id: "version-1",
        active: true,
        index_status: "pending",
        indexed: false,
      }),
    );

    const result = await activateSkill("sk_1", "version-1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/skills/sk_1/activate");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ version_id: "version-1" });
    expect(result.active).toBe(true);
    expect(result.index_status).toBe("pending");
  });

  it("setSkillPreference persists the DB switch", async () => {
    const fetchMock = stubFetch(jsonResponse({ ...SKILL, enabled: false }));

    const skill = await setSkillPreference("sk_1", false);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/skills/sk_1/preference");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ enabled: false });
    expect(skill.enabled).toBe(false);
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

  it("turns a coded scanner error body into SkillApiError", async () => {
    stubFetch(
      jsonResponse(
        {
          code: "skill_scanner_unavailable",
          detail: "cisco-ai-skill-scanner is not installed",
          trace_id: "trace-9",
        },
        503,
      ),
    );

    const error = await scanSkill("sk_1").catch((reason: unknown) => reason);

    expect(error).toBeInstanceOf(SkillApiError);
    const apiError = error as SkillApiError;
    expect(apiError.status).toBe(503);
    expect(apiError.code).toBe("skill_scanner_unavailable");
    expect(apiError.traceId).toBe("trace-9");
  });
});
