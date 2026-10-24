import type { SkillStatus } from "@/core/api/skills";
import { cn } from "@/lib/utils";

/**
 * 技能状态徽标（纯展示：不取数、不带副作用）。
 *
 * 状态词表是后端定的（draft/pending/approved/rejected/withdrawn），这里只负责
 * 翻译成人话并配一个颜色。页面、编辑器、审核队列都要画同一个状态，所以抽在
 * 一处——三处各写一份的话，"pending" 迟早有三个人话版本。
 */
const LABELS: Record<SkillStatus, string> = {
  draft: "草稿",
  pending: "审核中",
  approved: "已通过",
  rejected: "已驳回",
  withdrawn: "已撤回",
};

const TONES: Record<SkillStatus, string> = {
  draft: "border-neutral-200 bg-neutral-50 text-neutral-600",
  pending: "border-amber-200 bg-amber-50 text-amber-800",
  approved: "border-emerald-200 bg-emerald-50 text-emerald-700",
  rejected: "border-red-200 bg-red-50 text-red-700",
  withdrawn: "border-neutral-200 bg-neutral-100 text-neutral-500",
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
