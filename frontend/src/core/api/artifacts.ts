import { ARTIFACT_PREVIEW_MAX_BYTES } from "../types";
import { apiUrl } from "./base";

/**
 * Artifact 预览（M5 §6.5）。
 *
 * - `ThreadState.artifacts` 是权威的清单
 * - 文本内容通过 **HTTP byte range 只取前 1 MiB**，后端返回 206
 * - **被截断的预览要保持轻量**，给一个"加载完整文件"的显式操作，
 *   不要为它挂载重型代码编辑器
 */

export interface ArtifactPreview {
  path: string;
  text: string;
  /** 服务端返回了 206 / 内容长度超上限。UI 据此显示"加载完整文件" */
  truncated: boolean;
  /** 从 `Content-Range` 解析出的总字节数，拿不到就是 null */
  totalBytes: number | null;
}

export function parseContentRange(header: string | null): number | null {
  if (!header) return null;
  const match = /\/(\d+)\s*$/.exec(header);
  if (!match) return null;
  const total = Number.parseInt(match[1]!, 10);
  return Number.isFinite(total) ? total : null;
}

/**
 * 路径是 **URL 的一段**，不是 query 参数：后端路由是
 * `GET /api/threads/{tid}/artifacts/{path:path}`（router-layer 文档 §路由表）。
 *
 * 用 `path:path` 转换器意味着斜杠是路径的一部分，所以**逐段编码**、
 * 保留 `/`——整段 encodeURIComponent 会把斜杠也编码掉，直接 404。
 */
export function artifactContentUrl(threadId: string, path: string, download = false): string {
  const encodedPath = path
    .split("/")
    .filter(Boolean)
    .map(encodeURIComponent)
    .join("/");
  const query = download ? "?download=true" : "";
  return apiUrl(`/threads/${encodeURIComponent(threadId)}/artifacts/${encodedPath}${query}`);
}

export interface FetchArtifactOptions {
  /** `limit` 传 `null` 表示"加载完整文件"（那个显式操作） */
  limitBytes?: number | null;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

export async function fetchArtifactPreview(
  threadId: string,
  path: string,
  options: FetchArtifactOptions = {},
): Promise<ArtifactPreview> {
  const { limitBytes = ARTIFACT_PREVIEW_MAX_BYTES, signal, fetchImpl = fetch } = options;
  const headers: Record<string, string> = {};
  if (limitBytes !== null) headers.Range = `bytes=0-${limitBytes - 1}`;

  const response = await fetchImpl(artifactContentUrl(threadId, path), { headers, signal });
  if (!response.ok) {
    throw new Error(`Failed to load artifact ${path}: HTTP ${response.status}`);
  }

  const text = await response.text();
  // 后端返回 206 就是被截断了；有些代理会把它降级成 200，用长度兜底
  const partial = response.status === 206;
  return {
    path,
    text,
    truncated: partial || (limitBytes !== null && text.length >= limitBytes),
    totalBytes: parseContentRange(response.headers.get("Content-Range")),
  };
}
