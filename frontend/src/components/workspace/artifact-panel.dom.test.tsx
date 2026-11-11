import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ARTIFACT_PANEL_STORAGE_KEY, ArtifactPanel } from "./artifact-panel";

/**
 * §6.5 的两条状态规则只有在真实 React 下才存在（effect 顺序 + sessionStorage），
 * 所以写成 DOM 测试，而不是把 React mock 掉的 node 测试（M5 §8.1）。
 *
 * 这里不测"预览长什么样"（那是纯展示，看一眼就知道），只测那个最容易写错的：
 * **初始的空流值不能在历史加载完之前覆盖恢复出来的状态。**
 */

function storedState(): { open: boolean; path: string | null } | null {
  const raw = window.sessionStorage.getItem(ARTIFACT_PANEL_STORAGE_KEY);
  return raw ? (JSON.parse(raw) as { open: boolean; path: string | null }) : null;
}

function previewOf(path: string) {
  return { path, text: "内容", truncated: false, totalBytes: 4 };
}

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(cleanup);

describe("artifact 面板的 UI 状态（sessionStorage）", () => {
  it("恢复出来的开关和选中路径不会被挂载瞬间的空默认值冲掉", async () => {
    window.sessionStorage.setItem(
      ARTIFACT_PANEL_STORAGE_KEY,
      JSON.stringify({ open: true, path: "a.txt" }),
    );
    const loadPreview = vi.fn(async (path: string) => previewOf(path));

    render(<ArtifactPanel artifacts={["a.txt", "b.txt"]} loadPreview={loadPreview} />);

    await waitFor(() => {
      expect(loadPreview).toHaveBeenCalled();
    });
    expect(loadPreview.mock.calls[0]?.[0]).toBe("a.txt");
    expect(storedState()).toEqual({ open: true, path: "a.txt" });
  });

  it("历史加载完之前，空的产物清单不会清掉恢复出来的选中路径", async () => {
    window.sessionStorage.setItem(
      ARTIFACT_PANEL_STORAGE_KEY,
      JSON.stringify({ open: true, path: "old.txt" }),
    );
    const loadPreview = vi.fn(async (path: string) => previewOf(path));

    const view = render(<ArtifactPanel artifacts={[]} loadPreview={loadPreview} threadLoading />);
    // 空清单不是"用户改了选择"，不许写回 storage
    expect(storedState()?.path).toBe("old.txt");
    // 历史还没到，不能断言"没有产物"：此时整块面板都不渲染
    expect(view.queryByRole("complementary", { name: "产物" })).toBeNull();

    // 真实清单到达，恢复出来的路径不在里面，面板落到第一个上
    view.rerender(<ArtifactPanel artifacts={["new.txt"]} loadPreview={loadPreview} />);
    await waitFor(() => {
      expect(loadPreview).toHaveBeenCalledWith("new.txt", expect.anything());
    });
    // 这条是**派生**的，不是用户操作，所以仍然不写回
    expect(storedState()?.path).toBe("old.txt");
  });

  it("用户显式选了一个产物时才写回 storage", async () => {
    const loadPreview = vi.fn(async (path: string) => previewOf(path));

    const view = render(<ArtifactPanel artifacts={["a.txt", "b.txt"]} loadPreview={loadPreview} />);
    // 收起态只有一个按钮，它的可访问名带上角标计数（"▸ 产物 2"）
    fireEvent.click(view.getByRole("button", { name: /产物/ }));

    await waitFor(() => {
      expect(storedState()).toEqual({ open: true, path: "a.txt" });
    });
  });

  it("选中产物时提供带 download=true 的下载链接", async () => {
    window.sessionStorage.setItem(
      ARTIFACT_PANEL_STORAGE_KEY,
      JSON.stringify({ open: true, path: "flow-field-particles.html" }),
    );
    const loadPreview = vi.fn(async (path: string) => previewOf(path));
    const artifactDownloadUrl = vi.fn(
      (path: string) => `/api/threads/t1/artifacts/${path}?download=true`,
    );

    const view = render(
      <ArtifactPanel
        artifactDownloadUrl={artifactDownloadUrl}
        artifacts={["flow-field-particles.html"]}
        loadPreview={loadPreview}
      />,
    );

    const link = await view.findByRole("link", { name: "下载 flow-field-particles.html" });
    expect(link.getAttribute("href")).toBe(
      "/api/threads/t1/artifacts/flow-field-particles.html?download=true",
    );
    expect(link.hasAttribute("download")).toBe(true);
    expect(artifactDownloadUrl).toHaveBeenCalledWith("flow-field-particles.html");
  });
});
