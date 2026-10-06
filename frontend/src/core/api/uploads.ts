import { apiUrl } from "./base";

/**
 * 文件上传。
 *
 * **假设说明（写在这里，因为设计文档还没定死）：** M2 定了目录布局
 * `users/{uid}/threads/{tid}/user-data/{workspace,uploads,outputs}`，
 * M6 的 `UploadsMiddleware` 负责"把新上传的文件清单注入对话（只 hook 一次，
 * 写进 `uploaded_files` 通道）"，但**上传的 HTTP 端点本身没有在任何设计文档里
 * 出现过**（M9 才可能加）。
 *
 * 所以这里把路径收在一个常量里，端点定了只改这一行。
 * 服务端返回 404/405 时会抛出可读的错误，而不是静默丢弃文件——
 * 静默丢弃会让用户以为文件传上去了，模型却看不到它。
 */
export const UPLOAD_PATH = (threadId: string) =>
  `/threads/${encodeURIComponent(threadId)}/uploads`;

export interface UploadedFileRef {
  path: string;
  name?: string;
  size?: number;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;

export function parseUploadResponse(payload: unknown): UploadedFileRef[] {
  const raw = isRecord(payload) && Array.isArray(payload.files)
    ? payload.files
    : Array.isArray(payload)
      ? payload
      : null;
  if (!raw) throw new Error("Unexpected upload response: expected an array or { files: [...] }");

  return raw.filter(isRecord).flatMap((item): UploadedFileRef[] => {
    const path =
      typeof item.path === "string" ? item.path : typeof item.virtual_path === "string" ? item.virtual_path : null;
    if (!path) return [];
    const ref: UploadedFileRef = { path };
    if (typeof item.name === "string") ref.name = item.name;
    if (typeof item.size === "number") ref.size = item.size;
    return [ref];
  });
}

export interface UploadOptions {
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
}

export async function uploadFiles(
  threadId: string,
  files: readonly File[],
  options: UploadOptions = {},
): Promise<UploadedFileRef[]> {
  if (!files.length) return [];
  const { signal, fetchImpl = fetch } = options;

  const form = new FormData();
  for (const file of files) form.append("files", file, file.name);

  const response = await fetchImpl(apiUrl(UPLOAD_PATH(threadId)), {
    method: "POST",
    body: form,
    signal,
    // 不要手动设 Content-Type：boundary 由浏览器生成
  });
  if (!response.ok) {
    throw new Error(`Upload failed for thread ${threadId}: HTTP ${response.status}`);
  }
  return parseUploadResponse(await response.json());
}
