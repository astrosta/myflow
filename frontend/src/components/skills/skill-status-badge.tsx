import type { SkillStatus } from "@/core/api/skills";
import { cn } from "@/lib/utils";

/**
 * 技能生命周期徽标（纯展示：不取数、不带副作用）。
 *
 * 词表由后端派生：draft / unscanned / scan_failed / scanned / active / builtin。
 * 页面与编辑器共用，避免同一状态出现多份翻译。
 */
const LABELS: Record<SkillStatus, string> = {
  draft: "草稿",
  unscanned: "草稿 · 待扫描",
  scan_failed: "草稿 · 扫描未通过",
  scanned: "草稿 · 扫描通过",
  active: "已生效",
  builtin: "内置",
};

const TONES: Record<SkillStatus, string> = {
  draft: "border-neutral-200 bg-neutral-50 text-neutral-600",
  unscanned: "border-sky-200 bg-sky-50 text-sky-700",
  scan_failed: "border-red-200 bg-red-50 text-red-700",
  scanned: "border-amber-200 bg-amber-50 text-amber-800",
  active: "border-emerald-200 bg-emerald-50 text-emerald-700",
  builtin: "border-neutral-300 bg-neutral-100 text-neutral-600",
};

export function skillStatusLabel(status: SkillStatus): string {
  return LABELS[status] ?? status;
}

export interface SkillStatusBadgeProps {
  status: SkillStatus;
  className?: string;
}

export function SkillStatusBadge({ status, className }: SkillStatusBadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 text-[11px] leading-none",
        TONES[status] ?? TONES.draft,
        className,
      )}
    >
      {skillStatusLabel(status)}
    </span>
  );
}
