"use client";

import { useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { Textarea } from "@/components/ui/textarea";
import {
  activateSkill,
  deleteSkill,
  getDraft,
  listDraftFiles,
  readDraftFile,
  saveDraft,
  SkillApiError,
  uploadDraftFiles,
  writeDraftFile,
  type SecurityFinding,
  type SecurityScan,
} from "@/core/api/skills";

import { SkillEditorActions } from "./skill-editor-actions";
import {
  clearSkillScanRun,
  startSkillScanRun,
  useSkillScanRun,
} from "./skill-scan-run";
import { SkillFileTree } from "./skill-file-tree";
import { skillIndexStatusLabel } from "./skill-index-status";
import { SkillStatusBadge } from "./skill-status-badge";

export interface SkillEditorPageProps {
  skillId: string;
}

/** 所有草稿相关查询都挂在这个前缀下，一次失效就把树、正文、扫描一起刷新。 */
const draftKey = (skillId: string) => ["skills", "draft", skillId] as const;

type Busy = "save" | "scan" | "activate" | "delete" | "upload" | null;

function readableError(error: unknown): string {
  if (error instanceof SkillApiError) {
    return error.detail || "请求失败（HTTP " + error.status + "）";
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

/** 严重性降序：CRITICAL > HIGH > MEDIUM > LOW > INFO > SAFE，未知值排最后。 */
const SEVERITY_RANK: Record<string, number> = {
  CRITICAL: 5,
  HIGH: 4,
  MEDIUM: 3,
  LOW: 2,
  INFO: 1,
  SAFE: 0,
};

const SEVERITY_TONE: Record<string, string> = {
  CRITICAL: "bg-red-100 text-red-700",
  HIGH: "bg-orange-100 text-orange-700",
  MEDIUM: "bg-amber-100 text-amber-700",
  LOW: "bg-sky-100 text-sky-700",
  INFO: "bg-neutral-100 text-neutral-600",
  SAFE: "bg-emerald-100 text-emerald-700",
};

/** 滑动窗口一次显示几组发现。 */
const FINDINGS_PER_WINDOW = 3;
/** 每组等高 + 固定视口高度：滚动时窗口里永远整整齐齐是 3 组，不会露出半张卡。 */
const FINDING_CARD_HEIGHT = "6rem";
const FINDING_GAP = "0.5rem";
const FINDINGS_WINDOW_HEIGHT = `calc(${FINDING_CARD_HEIGHT} * ${FINDINGS_PER_WINDOW} + ${FINDING_GAP} * ${FINDINGS_PER_WINDOW - 1})`;

function severityRank(severity: string): number {
  return SEVERITY_RANK[severity.toUpperCase()] ?? -1;
}

/** 码元序比较：规则 id 是标识符不是自然语言，localeCompare 的忽略标点会打乱顺序。 */
function compareText(left: string, right: string): number {
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

/** 严重性降序；同级别按规则名、文件、行号稳定排序，滑动顺序才可预期。 */
function sortFindings(findings: readonly SecurityFinding[]): SecurityFinding[] {
  return [...findings].sort((left, right) => {
    const bySeverity = severityRank(right.severity) - severityRank(left.severity);
    if (bySeverity !== 0) return bySeverity;
    return (
      compareText(left.rule_id, right.rule_id) ||
      compareText(left.file_path ?? "", right.file_path ?? "") ||
      (left.line_number ?? 0) - (right.line_number ?? 0)
    );
  });
}

function FindingCard({ finding }: { finding: SecurityFinding }) {
  const tone = SEVERITY_TONE[finding.severity.toUpperCase()] ?? "bg-neutral-100 text-neutral-600";
  return (
    <div className="h-full overflow-hidden rounded border border-neutral-200 bg-white px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className={"rounded px-1.5 py-0.5 font-mono text-[10px] " + tone}>
          {finding.severity}
        </span>
        <span className="text-xs font-medium text-neutral-800">{finding.title}</span>
        <span className="font-mono text-[10px] text-neutral-400">{finding.rule_id}</span>
      </div>
      {finding.file_path ? (
        <p className="mt-1 font-mono text-[11px] text-neutral-500">
          {finding.file_path}
          {finding.line_number ? ":" + finding.line_number : ""}
        </p>
      ) : null}
      {finding.remediation ? (
        <p className="mt-1 text-[11px] leading-relaxed text-neutral-600">{finding.remediation}</p>
      ) : null}
    </div>
  );
}

/**
 * 发现的滑动窗口：窗口高度固定为 3 组，其余内容上下滚动查看。
 *
 * 顺序按严重性降序（同级别按规则名/文件/行号），所以窗口顶部永远是最该先看的那条；
 * 卡片等高 + 固定视口高度，滚动时不会出现"半张卡"，snap 让每一停都对齐一组。
 */
function FindingCarousel({ scan }: { scan: SecurityScan }) {
  const ordered = useMemo(() => sortFindings(scan.findings), [scan.findings]);
  const total = ordered.length;
  const overflow = Math.max(0, total - FINDINGS_PER_WINDOW);

  return (
    <div className="mt-3" role="group" aria-label="扫描发现">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-[11px] text-neutral-500">
          发现按严重性排序 · 共 {total} 组
          {overflow > 0 ? "，上下滑动查看其余 " + overflow + " 组" : ""}
        </p>
        <span className="text-[10px] text-neutral-400">窗口 {FINDINGS_PER_WINDOW} 组</span>
      </div>
      <ul
        aria-label="扫描发现列表"
        className={cn(
          "mt-2 space-y-2 overflow-y-auto pr-1 snap-y snap-proximity",
          "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-neutral-300",
        )}
        style={{ height: FINDINGS_WINDOW_HEIGHT }}
        tabIndex={overflow > 0 ? 0 : -1}
      >
        {ordered.map((finding) => (
          <li
            className="snap-start"
            key={finding.rule_id + ":" + (finding.file_path ?? "") + ":" + (finding.line_number ?? "")}
            style={{ height: FINDING_CARD_HEIGHT }}
          >
            <FindingCard finding={finding} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function MetaItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-[11px] text-neutral-400">{label}</dt>
      <dd className="mt-0.5 truncate font-mono text-xs text-neutral-700" title={value}>
        {value}
      </dd>
    </div>
  );
}

function ScanPanel({ scan }: { scan: SecurityScan | null }) {
  if (!scan) {
    return (
      <p className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-800">
        当前草稿尚未做安全扫描（规则 + LLM）。保存后点击「安全扫描」，通过后才能生效。
      </p>
    );
  }
  const failed = scan.status !== "completed" || !scan.is_safe;
  return (
    <section
      className={
        "mt-3 rounded-lg border px-4 py-3 " +
        (failed ? "border-red-200 bg-red-50/70" : "border-emerald-200 bg-emerald-50/60")
      }
      aria-label="安全扫描结果"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h2 className="text-xs font-semibold text-neutral-800">安全扫描</h2>
          <span
            className={
              "rounded-full border px-2 py-0.5 text-[11px] " +
              (failed
                ? "border-red-300 bg-white text-red-700"
                : "border-emerald-300 bg-white text-emerald-700")
            }
          >
            {scan.status === "failed" ? "执行失败" : scan.is_safe ? "通过" : "未通过"}
          </span>
        </div>
        <span className="text-[11px] text-neutral-500">
          最高 {scan.max_severity} · {scan.findings_count} 个发现 · {scan.duration_ms} ms
        </span>
      </div>
      <p className="mt-1 text-[11px] text-neutral-500">
        {scan.scanner}
        {scan.scanner_version ? " " + scan.scanner_version : ""} ·{" "}
        {new Date(scan.created_at).toLocaleString()}
      </p>
      {scan.error ? (
        <p className="mt-2 rounded border border-red-200 bg-white px-2 py-1.5 font-mono text-[11px] text-red-700">
          {scan.error}
        </p>
      ) : null}
      {scan.findings.length > 0 ? (
        // key 换成 scan_id：重新扫描之后窗口回到第 1 页，不会停在上一次的偏移上。
        <FindingCarousel key={scan.scan_id} scan={scan} />
      ) : null}
    </section>
  );
}

/**
 * 私有技能编辑器：文件树 + 正文 + 保存 / 安全扫描 / 生效 / 删除。
 *
 * 保存只产生草稿版本；扫描针对最新已保存版本；生效把最新版本设为 active 并
 * 写入索引 outbox。正文和选中路径是本地状态，不点保存永远不写后端。
 */
export function SkillEditorPage({ skillId }: SkillEditorPageProps) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [selectedPath, setSelectedPath] = useState("SKILL.md");
  const [edit, setEdit] = useState<{ path: string; text: string } | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // 上传会立即写入暂存目录，但只有 save 才生成版本；先记下来，避免上传后
  // 因为没有正文改动而无法保存，也避免扫描/生效越过未保存的附件。
  const [pendingUpload, setPendingUpload] = useState(false);

  const draftQuery = useQuery({
    queryKey: draftKey(skillId),
    queryFn: () => getDraft(skillId),
  });
  const filesQuery = useQuery({
    queryKey: [...draftKey(skillId), "files"],
    queryFn: () => listDraftFiles(skillId),
  });
  const fileQuery = useQuery({
    queryKey: [...draftKey(skillId), "file", selectedPath],
    queryFn: () => readDraftFile(skillId, selectedPath),
    enabled: selectedPath.length > 0,
  });

  const serverText = fileQuery.data?.content ?? "";
  const text = edit?.path === selectedPath ? edit.text : serverText;
  const dirty = text !== serverText;
  const setText = (next: string) => setEdit({ path: selectedPath, text: next });

  const draft = draftQuery.data;
  const files = filesQuery.data ?? draft?.files ?? [];
  const pageError = draftQuery.error ?? filesQuery.error;
  const loading = draftQuery.isLoading || filesQuery.isLoading;
  const scan = draft?.latest_scan ?? null;
  // 扫描状态挂在组件外：切到其他页面再回来时，仍能看见扫描中并阻止重复扫描。
  const scanRun = useSkillScanRun(skillId);
  const scanning = scanRun.status === "running";
  const effectiveBusy = scanning ? "scan" : busy;
  const scanError = scanRun.status === "failed" ? readableError(scanRun.error) : null;
  const hasUnsavedChanges = dirty || pendingUpload;
  // 新技能还没有任何版本：即使正文没改，也需要第一次保存来生成 v1。
  const canSave = hasUnsavedChanges || !draft?.latest_version;
  const canScan = !hasUnsavedChanges && Boolean(draft?.latest_version);
  const canActivate = Boolean(draft?.skill.can_activate) && !hasUnsavedChanges;

  const invalidateDraft = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["skills", "mine"] }),
      queryClient.invalidateQueries({ queryKey: ["skills", "catalog"] }),
      queryClient.invalidateQueries({ queryKey: draftKey(skillId) }),
    ]);
  };

  const begin = (kind: Exclude<Busy, null>) => {
    setBusy(kind);
    setActionError(null);
    setNotice(null);
    clearSkillScanRun(skillId);
  };

  const selectPath = (path: string) => {
    setSelectedPath(path);
    // 换文件 = 放弃上一份未保存的本地内容（不静默写盘）。
    setEdit(null);
    setActionError(null);
  };

  const save = async () => {
    if (effectiveBusy || !canSave) return;
    begin("save");
    try {
      if (dirty) {
        await writeDraftFile(skillId, { path: selectedPath, content: text });
      }
      await saveDraft(skillId);
      setEdit(null);
      setPendingUpload(false);
      await invalidateDraft();
      setNotice("已保存草稿。运行安全扫描后才能生效。");
    } catch (cause) {
      setActionError(readableError(cause));
    } finally {
      setBusy(null);
    }
  };

  const upload = async (selected: File[]) => {
    if (effectiveBusy) return;
    begin("upload");
    try {
      await uploadDraftFiles(skillId, selected);
      setPendingUpload(true);
      await invalidateDraft();
      setNotice("已上传 " + selected.length + " 个文件，保存草稿后才能扫描和生效。");
    } catch (cause) {
      setActionError(readableError(cause));
    } finally {
      setBusy(null);
    }
  };

  const scanNow = async () => {
    if (effectiveBusy) return;
    if (hasUnsavedChanges) {
      setNotice(null);
      setActionError("有未保存的修改，请先保存，再运行安全扫描。");
      return;
    }
    if (!draft?.latest_version) {
      setNotice(null);
      setActionError("技能尚未保存，请先保存，再运行安全扫描。");
      return;
    }
    begin("scan");
    try {
      // 扫描任务自己负责跨导航保留状态，并在完成后刷新草稿查询。
      const result = await startSkillScanRun(skillId, queryClient);
      setNotice(
        result.status === "failed"
          ? "安全扫描执行失败，请查看错误后重试。"
          : result.is_safe
            ? "安全扫描通过，可以点击「生效」。"
            : "安全扫描未通过，请修复发现的问题后重新扫描。",
      );
    } catch {
      // 失败状态由 skill-scan-run 保留，返回本页后仍能看到原因。
    } finally {
      setBusy(null);
    }
  };

  const activate = async () => {
    if (effectiveBusy) return;
    if (!draft?.skill.can_activate) {
      setNotice(null);
      setActionError(draft?.skill.activation_blocked_reason || "当前版本还不能生效。");
      return;
    }
    if (hasUnsavedChanges) {
      setNotice(null);
      setActionError("有未保存的修改，请先保存并重新扫描。");
      return;
    }
    begin("activate");
    try {
      await activateSkill(skillId, draft.skill.version_id);
      await invalidateDraft();
      // 生效成功后直接回列表，让用户在列表里看到新的 active 状态。
      router.push("/workspace/skills");
    } catch (cause) {
      setActionError(readableError(cause));
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    if (effectiveBusy) return;
    begin("delete");
    try {
      await deleteSkill(skillId);
      await queryClient.invalidateQueries({ queryKey: ["skills", "mine"] });
      await queryClient.invalidateQueries({ queryKey: ["skills", "catalog"] });
      router.push("/workspace/skills");
    } catch (cause) {
      setActionError(readableError(cause));
      setBusy(null);
    }
  };

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto w-full max-w-6xl px-5 py-6 sm:px-8">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-neutral-200 pb-5">
          <div className="min-w-0">
            <Link
              className="text-[11px] text-neutral-500 hover:text-neutral-900"
              href="/workspace/skills"
            >
              ← 返回技能中心
            </Link>
            <div className="mt-1 flex flex-wrap items-center gap-2">
              <h1 className="truncate text-xl font-semibold tracking-tight text-neutral-900">
                {draft?.skill.name || "技能编辑器"}
              </h1>
              {draft ? <SkillStatusBadge status={draft.skill.status} /> : null}
            </div>
            <p className="mt-1 text-xs leading-relaxed text-neutral-700">
              {draft?.skill.description || "在 SKILL.md 的 frontmatter 中填写技能描述。"}
            </p>
            <p className="mt-1 text-[11px] leading-relaxed text-neutral-400">
              技能名称和描述来自 SKILL.md。技能只有你自己可见；保存后扫描通过并生效才会进入运行时与检索。
            </p>
          </div>
        </header>

        {loading ? (
          <div className="mt-5 space-y-3" aria-busy="true" aria-label="加载技能草稿">
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-96 w-full" />
          </div>
        ) : null}

        {pageError ? (
          <p
            className="mt-5 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700"
            role="alert"
          >
            {readableError(pageError)}
          </p>
        ) : null}

        {draft && !pageError ? (
          <>
            <dl className="mt-5 grid grid-cols-2 gap-3 rounded-lg border border-neutral-200 bg-neutral-50/60 px-4 py-3 sm:grid-cols-4">
              <MetaItem
                label="当前草稿版本"
                value={draft.latest_version?.skill_version ?? "—"}
              />
              <MetaItem
                label="草稿提交"
                value={draft.latest_version?.draft_commit?.slice(0, 12) ?? "—"}
              />
              <MetaItem
                label="索引状态"
                value={skillIndexStatusLabel(draft.skill.index_status)}
              />
              <MetaItem
                label="已生效版本"
                value={draft.skill.active_version_id ? draft.skill.active_version_id.slice(0, 12) : "—"}
              />
            </dl>

            {draft.skill.active_version_id &&
            draft.skill.active_version_id !== draft.skill.version_id ? (
              <p className="mt-3 rounded-lg border border-sky-200 bg-sky-50 px-4 py-3 text-xs text-sky-800" role="status">
                已生效版本仍是旧的；当前草稿需要重新扫描并通过后才能覆盖运行时版本。
              </p>
            ) : null}

            {draft.skill.activation_blocked_reason ? (
              <p className="mt-3 text-[11px] text-neutral-500">
                {draft.skill.activation_blocked_reason}
              </p>
            ) : null}

            {actionError || scanError ? (
              <p
                className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
                role="alert"
              >
                {actionError ?? scanError}
              </p>
            ) : null}

            {notice ? (
              <p
                className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-700"
                role="status"
              >
                {notice}
              </p>
            ) : null}

            <div className="mt-5 grid gap-4 lg:grid-cols-[15rem_minmax(0,1fr)]">
              <SkillFileTree
                disabled={effectiveBusy !== null}
                files={files}
                onSelect={selectPath}
                onUpload={(selected) => void upload(selected)}
                selectedPath={selectedPath}
                uploading={effectiveBusy === "upload"}
              />

              <section className="min-w-0 rounded-lg border border-neutral-200 bg-white p-3">
                <div className="flex items-center justify-between gap-2">
                  <h2 className="truncate font-mono text-xs font-medium text-neutral-700">
                    {selectedPath}
                  </h2>
                  {fileQuery.isFetching ? (
                    <span className="text-[11px] text-neutral-400">加载中…</span>
                  ) : dirty ? (
                    <span className="text-[11px] text-amber-700">未保存的修改</span>
                  ) : null}
                </div>

                {fileQuery.error ? (
                  <p
                    className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700"
                    role="alert"
                  >
                    {readableError(fileQuery.error)}
                  </p>
                ) : fileQuery.isLoading ? (
                  <Skeleton className="mt-3 h-80 w-full" />
                ) : (
                  <label className="mt-2 block" htmlFor="skill-file-content">
                    <span className="sr-only">文件内容</span>
                    <Textarea
                      aria-label="文件内容"
                      className="min-h-[24rem] font-mono text-xs"
                      disabled={effectiveBusy !== null}
                      id="skill-file-content"
                      onChange={(event) => setText(event.target.value)}
                      spellCheck={false}
                      value={text}
                    />
                  </label>
                )}

                {scanning ? (
                  <p className="mt-3 text-xs text-neutral-600" role="status">
                    {scanRun.message ?? "正在准备安全扫描…"}
                  </p>
                ) : null}

                <ScanPanel scan={scan} />

                <div className="mt-3">
                  <SkillEditorActions
                    busy={effectiveBusy}
                    canActivate={canActivate}
                    canSave={canSave}
                    canScan={canScan}
                    onActivate={() => void activate()}
                    onDelete={() => void remove()}
                    onSave={() => void save()}
                    onScan={() => void scanNow()}
                  />
                </div>
              </section>
            </div>
          </>
        ) : null}
      </div>
    </div>
  );
}
