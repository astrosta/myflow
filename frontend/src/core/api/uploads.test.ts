import { describe, expect, it } from "vitest";

import { parseUploadResponse } from "./uploads";

describe("parseUploadResponse", () => {
  it("keeps the stored filename and size from the gateway response", () => {
    const files = parseUploadResponse({
      files: [
        {
          filename: "report_2.pdf",
          original_filename: "report.pdf",
          size: 12,
          virtual_path: "/mnt/user-data/uploads/report_2.pdf",
          modified: "2026-11-04T00:00:00Z",
        },
      ],
    });

    expect(files).toEqual([
      {
        path: "/mnt/user-data/uploads/report_2.pdf",
        filename: "report_2.pdf",
        size: 12,
      },
    ]);
  });

  it("falls back to a legacy name or the path basename", () => {
    expect(
      parseUploadResponse([
        { path: "/mnt/user-data/uploads/a.txt", name: "a.txt" },
        { path: "/mnt/user-data/uploads/b.txt" },
      ]),
    ).toEqual([
      { path: "/mnt/user-data/uploads/a.txt", filename: "a.txt", name: "a.txt" },
      { path: "/mnt/user-data/uploads/b.txt", filename: "b.txt" },
    ]);
  });
});
