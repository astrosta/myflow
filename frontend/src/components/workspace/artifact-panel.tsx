"use client";

import { useCallback, useEffect, useMemo, useState, useSyncExternalStore } from "react";

import { Badge } from "@/components/ui/badge";
import type { ArtifactPreview } from "@/core/api/artifacts";
import { cn } from "@/lib/utils";

import { ArtifactPreview as ArtifactPreviewView, type ArtifactPreviewState } from "./artifact-preview";

/**
 * Artifact 清单 + 只读预览（M5 §6.5）。
 *
 * 关于状态的两条硬要求，改这个文件前务必读过：
 *
 * 1. **面板的 UI 状态（开关、选中路径）存 sessionStorage。**
 * 2. **初始的空流值不能在历史加载完之前覆盖恢复出来的状态。**
 *
 * 第 2 条在实现上是**结构性**的，不靠"记得判断"：
 *
 *   - 恢复走 `useSyncExternalStore`，快照在渲染期直接读出来，**不进 state**。
 *     没有"先渲染成默认值、再在 effect 里改成恢复值"的那个中间态，
 *     也就没有窗口能让空值把恢复值冲掉。
 *   - 写回 storage 只发生在**用户真的操作了**的时候（`applyPanelState`），
 *     不再有一个"挂载后把当前值写回去"的 effect——那个 effect 正是
 *     原始 bug 的形状：它在恢复完成前跑的话，写回去的就是空值。
 *   - "自动选中第一个"是**派生**的，不是 effect + setState。历史没加载完时
 *     派生函数直接放弃做决定（`threadLoading` 闸），不会用空清单把选中项清掉。
 *
 * 数据来源是**注入**进来的 loadPreview：面板自己不发请求，
 * 这样它和纯展示组件一样好测，也方便以后换成别的取数方式。
 */

export interface ArtifactPreviewLoadOptions {
  /** null = 加载完整文件（那个显式操作；默认只取前 1 MiB） */
  limitBytes?: number | null;
  signal?: AbortSignal;
}

export type ArtifactPreviewLoader = (
  path: string,
  options?: ArtifactPreviewLoadOptions,
) => Promise<ArtifactPreview>;

/** 由应用层构造下载 URL；组件本身不认识网关地址。 */
export type ArtifactDownloadUrlBuilder = (path: string) => string;

export const ARTIFACT_PANEL_STORAGE_KEY = "myflow.artifact-panel.v1";

interface StoredPanelState {
  open: boolean;
  path: string | null;
}

const EMPTY_PANEL_STATE: StoredPanelState = { open: false, path: null };
const IDLE_STATE: ArtifactPreviewState = { status: "idle" };

export function parseStoredPanelState(raw: string | null): StoredPanelState {
  if (!raw) return EMPTY_PANEL_STATE;
  try {
    const parsed = JSON.parse(raw) as Partial<StoredPanelState>;
    return {
      open: parsed.open === true,
      path: typeof parsed.path === "string" && parsed.path ? parsed.path : null,
    };
  } catch {
    // 解析失败只当作"没有存过"，不要让一个坏值把整个面板打挂
    return EMPTY_PANEL_STATE;
  }
}

function writeStoredPanelState(next: StoredPanelState): void {
  try {
    window.sessionStorage.setItem(ARTIFACT_PANEL_STORAGE_KEY, JSON.stringify(next));
  } catch {
    /* 配额或隐私模式：状态丢了不影响功能 */
  }
}

/**
 * sessionStorage 在同一个标签页里不会触发 `storage` 事件（那是跨标签页的），
 * 所以订阅本身几乎不会触发。它的作用是在 SSR → 客户端的切换点上
 * 让 React 重新读一次快照，而不是把服务端快照永远用下去。
 */
function subscribeSessionStorage(onChange: () => void): () => void {
  window.addEventListener("storage", onChange);
  return () => window.removeEventListener("storage", onChange);
}

function readSessionSnapshot(): string | null {
  try {
    return window.sessionStorage.getItem(ARTIFACT_PANEL_STORAGE_KEY);
  } catch {
    return null;
  }
}

const readServerSnapshot = (): string | null => null;

export interface ArtifactPanelProps {
  artifacts: readonly string[];
  /** 首屏历史还没加载完。加载完之前不要用空清单覆盖恢复出来的状态 */
  threadLoading?: boolean;
  loadPreview: ArtifactPreviewLoader;
  /** 选中产物的下载 URL；没注入时不显示下载入口。 */
  artifactDownloadUrl?: ArtifactDownloadUrlBuilder;
  className?: string;
}

export function ArtifactPanel({
  artifacts,
  threadLoading = false,
  loadPreview,
  artifactDownloadUrl,
  className,
}: ArtifactPanelProps) {
  const rawStored = useSyncExternalStore(
    subscribeSessionStorage,
    readSessionSnapshot,
    readServerSnapshot,
  );
  const restored = useMemo(() => parseStoredPanelState(rawStored), [rawStored]);

  // 用户这一次会话里的显式选择优先于恢复值；没操作过就是 null，直接沿用恢复值
  const [openChoice, setOpenChoice] = useState<boolean | null>(null);
  const [pathChoice, setPathChoice] = useState<string | null>(null);

  const open = openChoice ?? restored.open;

  /**
   * 选中项的派生。
   *
   * 历史还没加载完时**不做决定**：那时 `artifacts` 是空清单，
   * 拿它去判断"恢复出来的路径还有没有效"必然得出"无效"，
   * 于是恢复出来的选中项会被清掉——这正是 §6.5 点名的那条。
   */
  const selectedPath = useMemo(() => {
    const preferred = pathChoice ?? restored.path;
    if (preferred && artifacts.includes(preferred)) return preferred;
    if (preferred && threadLoading) return preferred;
    return artifacts[0] ?? null;
  }, [pathChoice, restored.path, artifacts, threadLoading]);

  const [fullLoaded, setFullLoaded] = useState(false);

  const applyPanelState = useCallback(
    (next: { open?: boolean; path?: string | null }) => {
      const nextOpen = next.open ?? open;
      const nextPath = next.path !== undefined ? next.path : selectedPath;
      if (next.open !== undefined) setOpenChoice(nextOpen);
      if (next.path !== undefined) setPathChoice(nextPath);
      // 只在用户真的操作时写回
      writeStoredPanelState({ open: nextOpen, path: nextPath });
    },
    [open, selectedPath],
  );

  const selectPath = useCallback(
    (path: string) => {
      // 换文件之后"已加载完整文件"不再成立，必须重置，否则新文件拿不到完整内容
      setFullLoaded(false);
      applyPanelState({ path });
    },
    [applyPanelState],
  );

  const activePath = open ? selectedPath : null;
  const requestKey = activePath ? `${activePath}::${fullLoaded ? "full" : "head"}` : null;

  /**
   * 取数结果带上它对应的 requestKey。
   * 这样"正在加载"是**派生**的（结果还没跟上请求），
   * 不需要在 effect 开头同步 `setState({status:"loading"})`——
   * 那会触发一次级联渲染。
   */
  const [loaded, setLoaded] = useState<{ key: string; state: ArtifactPreviewState } | null>(null);

  useEffect(() => {
    if (!activePath || !requestKey) return;
    const controller = new AbortController();
    loadPreview(activePath, {
      limitBytes: fullLoaded ? null : undefined,
      signal: controller.signal,
    })
      .then((preview) => {
        if (!controller.signal.aborted) {
          setLoaded({ key: requestKey, state: { status: "ready", preview } });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoaded({
          key: requestKey,
          state: {
            status: "error",
            message: error instanceof Error ? error.message : String(error),
          },
        });
      });
    return () => controller.abort();
  }, [activePath, requestKey, fullLoaded, loadPreview]);

  const state: ArtifactPreviewState =
    requestKey === null
      ? IDLE_STATE
      : loaded?.key === requestKey
        ? loaded.state
        : { status: "loading" };

  if (!artifacts.length) {
    /**
     * 没有产物就**什么都不画**。
     *
     * 这里曾经退化成一行"这一轮还没有产物。"（加载中是"正在加载产物…"），
     * 结果是每个没有文件的会话右侧都杵着一条无用的空白——用户会问
     * "这是什么，没有用就删掉"。历史还在加载时也无法断言"确实没有"，
     * 更不该先写一句占位。面板只在有内容可看时出现。
     */
    return null;
  }

  if (!open) {
    return (
      <aside
        aria-label="产物"
        className={cn(
          "flex items-start gap-2 border-t border-neutral-200 px-3 py-2 lg:border-l lg:border-t-0 lg:py-3",
          className,
        )}
      >
        <button
          className="inline-flex items-center gap-2 rounded-md px-2 py-1 text-xs font-medium text-neutral-600 transition-colors hover:bg-neutral-100 hover:text-neutral-900"
          onClick={() => applyPanelState({ open: true })}
          type="button"
        >
          <span aria-hidden>▸</span>
          产物
          <Badge variant="neutral">{artifacts.length}</Badge>
        </button>
      </aside>
    );
  }

  return (
    <aside
      aria-label="产物"
      className={cn(
        "flex min-h-0 flex-col border-t border-neutral-200 bg-neutral-50/40 lg:border-l lg:border-t-0",
        className,
      )}
    >
      <header className="flex items-center gap-2 border-b border-neutral-200 px-3 py-2">
        <h2 className="text-xs font-semibold text-neutral-700">产物</h2>
        <Badge variant="neutral">{artifacts.length}</Badge>
        <div className="ml-auto flex items-center gap-1">
          {selectedPath && artifactDownloadUrl ? (
            <a
              aria-label={`下载 ${selectedPath}`}
              className="rounded px-2 py-1 text-[11px] font-medium text-neutral-700 transition-colors hover:bg-neutral-100 hover:text-neutral-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-neutral-900/20"
              download
              href={artifactDownloadUrl(selectedPath)}
            >
              下载
            </a>
          ) : null}
          <button
            className="rounded px-2 py-1 text-[11px] text-neutral-500 transition-colors hover:bg-neutral-100 hover:text-neutral-800"
            onClick={() => applyPanelState({ open: false })}
            type="button"
          >
            收起
          </button>
        </div>
      </header>

      <ul className="max-h-40 shrink-0 overflow-auto border-b border-neutral-200 px-1 py-1 lg:max-h-56">
        {artifacts.map((path) => {
          const active = path === selectedPath;
          return (
            <li key={path}>
              <button
                aria-current={active ? "true" : undefined}
                className={cn(
                  "block w-full truncate rounded px-2 py-1.5 text-left font-mono text-[11px] transition-colors",
                  active
                    ? "bg-white text-neutral-900 shadow-sm"
                    : "text-neutral-600 hover:bg-white/70 hover:text-neutral-900",
                )}
                onClick={() => selectPath(path)}
                title={path}
                type="button"
              >
                {path}
              </button>
            </li>
          );
        })}
      </ul>

      <div className="min-h-0 flex-1 overflow-auto">
        <ArtifactPreviewView fullLoaded={fullLoaded} onLoadFull={() => setFullLoaded(true)} state={state} />
      </div>
    </aside>
  );
}
