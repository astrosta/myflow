"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { BookOpenIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import {
  createSkill,
  listMySkills,
  listSkills,
  setSkillPreference,
  SkillApiError,
  type SkillListItem,
} from "@/core/api/skills";

import { skillIndexStatusLabel } from "./skill-index-status";
import { SkillStatusBadge } from "./skill-status-badge";

/**
 * 「技能中心」：只展示当前用户自己的技能。
 *
 * - /skills/mine 是权威作者列表：草稿、待扫描、扫描未通过、已扫描、已生效都在这里；
 * - /skills 只用来取内置技能（内置于服务端，不写 Git/数据库）。
 *
 * 没有公共目录，也没有管理员审核入口。保存后的版本必须经过安全扫描并点击生效，
 * 才会出现在运行时与向量索引里。
 */

function readableError(error: unknown): string {
  if (error instanceof SkillApiError) {
    return error.detail || "请求失败（HTTP " + error.status + "）";
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

const editorHref = (skillId: string) => "/workspace/skills/" + encodeURIComponent(skillId);
const ROW_CLASS = "rounded-lg border border-neutral-200 bg-white p-4 transition-colors";

const NEW_SKILL_DESCRIPTION = "在 SKILL.md 中填写技能说明";

/** 新建时先落一个可编辑的占位技能；真正的名称和描述以 SKILL.md 为准。 */
function newSkillIdentity(): { name: string; description: string } {
  const suffix = Math.random().toString(36).slice(2, 10) || Date.now().toString(36);
  return {
    name: "new-skill-" + suffix,
    description: NEW_SKILL_DESCRIPTION,
  };
}

export function MySkillsPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [togglingId, setTogglingId] = useState<string | null>(null);
  const [toggleError, setToggleError] = useState<string | null>(null);

  const mineQuery = useQuery({
    queryKey: ["skills", "mine"],
    queryFn: listMySkills,
    staleTime: 5_000,
  });
  // 内置技能来自目录接口；目录里的用户行是 active 副本，这里只取内置。
  const builtinQuery = useQuery({
    queryKey: ["skills", "catalog"],
    queryFn: listSkills,
    staleTime: 5_000,
  });

  const mine = mineQuery.data ?? [];
  const drafts = mine.filter((skill) => skill.status !== "active");
  const activeSkills = mine.filter((skill) => skill.status === "active");
  const builtins = (builtinQuery.data ?? []).filter((skill) => skill.scope === "builtin");
  const error = mineQuery.error ?? builtinQuery.error;
  const loading = mineQuery.isLoading || builtinQuery.isLoading;
  const empty = !loading && !error && mine.length === 0 && builtins.length === 0;

  const openEditor = (skill: SkillListItem) => {
    router.push(editorHref(skill.skill_id));
  };

  const toggleEnabled = async (skill: SkillListItem) => {
    if (togglingId) return;
    setTogglingId(skill.skill_id);
    setToggleError(null);
    try {
      await setSkillPreference(skill.skill_id, !skill.enabled);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["skills", "mine"] }),
        queryClient.invalidateQueries({ queryKey: ["skills", "catalog"] }),
      ]);
    } catch (cause) {
      setToggleError(readableError(cause));
    } finally {
      setTogglingId(null);
    }
  };

  const create = async () => {
    if (creating) return;
    setCreating(true);
    setCreateError(null);
    try {
      const skill = await createSkill(newSkillIdentity());
      void queryClient.invalidateQueries({ queryKey: ["skills", "mine"] });
      void queryClient.invalidateQueries({ queryKey: ["skills", "catalog"] });
      openEditor(skill);
    } catch (cause) {
      setCreateError(readableError(cause));
    } finally {
      setCreating(false);
    }
  };

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto w-full max-w-5xl px-5 py-6 sm:px-8">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-neutral-200 pb-5">
          <div>
            <div className="flex items-center gap-2">
              <BookOpenIcon className="text-neutral-500" size={18} />
              <h1 className="text-xl font-semibold tracking-tight text-neutral-900">技能中心</h1>
            </div>
            <p className="mt-1 text-xs leading-relaxed text-neutral-500">
              技能按用户隔离。保存后先做安全扫描，扫描通过再点击生效。
            </p>
          </div>
          <div className="flex gap-2">
            <Button
              disabled={mineQuery.isFetching || builtinQuery.isFetching}
              onClick={() => {
                void mineQuery.refetch();
                void builtinQuery.refetch();
              }}
              size="sm"
              type="button"
            >
              刷新
            </Button>
            <Button
              disabled={creating}
              onClick={() => void create()}
              size="sm"
              type="button"
              variant="primary"
            >
              {creating ? "创建中…" : "＋ 新建技能"}
            </Button>
          </div>
        </header>

        {loading ? (
          <div className="mt-5 grid gap-3 sm:grid-cols-2" aria-busy="true" aria-label="加载技能列表">
            <Skeleton className="h-24 w-full" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : null}

        {error ? (
          <p
            className="mt-5 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700"
            role="alert"
          >
            {readableError(error)}
          </p>
        ) : null}

        {createError ? (
          <p
            className="mt-5 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700"
            role="alert"
          >
            {createError}
          </p>
        ) : null}

        {toggleError ? (
          <p
            className="mt-5 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700"
            role="alert"
          >
            {toggleError}
          </p>
        ) : null}

        {empty ? (
          <div className="mt-8 rounded-lg border border-dashed border-neutral-300 bg-neutral-50/60 px-6 py-12 text-center">
            <BookOpenIcon className="mx-auto text-neutral-300" size={28} />
            <p className="mt-3 text-sm font-medium text-neutral-700">还没有技能</p>
            <p className="mt-1 text-xs text-neutral-500">
              新建一个技能，保存后做安全扫描；扫描通过并点击生效后进入运行时与检索。
            </p>
            <Button
              className="mt-4"
              disabled={creating}
              onClick={() => void create()}
              size="sm"
              type="button"
              variant="primary"
            >
              {creating ? "创建中…" : "新建技能"}
            </Button>
          </div>
        ) : null}

        {drafts.length > 0 ? (
          <section aria-label="技能草稿" className="mt-6">
            <h2 className="text-xs font-medium text-neutral-500">技能草稿</h2>
            <ul className="mt-3 grid gap-3 sm:grid-cols-2">
              {drafts.map((skill) => (
                <SkillCard
                  key={skill.skill_id || skill.name}
                  onEdit={openEditor}
                  onToggle={toggleEnabled}
                  skill={skill}
                  toggling={togglingId === skill.skill_id}
                />
              ))}
            </ul>
          </section>
        ) : null}

        {activeSkills.length > 0 ? (
          <section aria-label="我的技能" className="mt-6">
            <h2 className="text-xs font-medium text-neutral-500">我的技能</h2>
            <ul className="mt-3 grid gap-3 sm:grid-cols-2">
              {activeSkills.map((skill) => (
                <SkillCard
                  key={skill.skill_id || skill.name}
                  onEdit={openEditor}
                  onToggle={toggleEnabled}
                  skill={skill}
                  toggling={togglingId === skill.skill_id}
                />
              ))}
            </ul>
          </section>
        ) : null}

        {builtins.length > 0 ? (
          <section className="mt-6">
            <h2 className="text-xs font-medium text-neutral-500">内置技能</h2>
            <ul className="mt-3 grid gap-3 sm:grid-cols-2">
              {builtins.map((skill) => (
                <li className={ROW_CLASS} key={skill.skill_id || skill.name}>
                  <h3 className="truncate text-sm font-semibold text-neutral-900">
                    {skill.name || "未命名技能"}
                  </h3>
                  <div className="mt-1 flex flex-wrap items-center gap-1.5">
                    <ScopeTag scope={skill.scope} />
                  </div>
                  {skill.description ? (
                    <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-neutral-500">
                      {skill.description}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        <p className="mt-8 text-[11px] text-neutral-400">
          技能只有你自己能看到、检索和使用。内置技能随服务端发布，只读。
        </p>
      </div>
    </div>
  );
}

function SkillCard({
  skill,
  onEdit,
  onToggle,
  toggling,
}: {
  skill: SkillListItem;
  onEdit: (skill: SkillListItem) => void;
  onToggle: (skill: SkillListItem) => void;
  toggling: boolean;
}) {
  return (
    <li className={ROW_CLASS}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-semibold text-neutral-900">
            {skill.name || "未命名技能"}
          </h3>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <ScopeTag scope={skill.scope} />
            {skill.source === "extracted" ? <ExtractedTag /> : null}
            <SkillStatusBadge status={skill.status} />
            {skill.skill_version ? (
              <span className="text-[11px] text-neutral-400">{skill.skill_version}</span>
            ) : null}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {skill.status === "active" ? (
            <SkillEnabledSwitch
              enabled={Boolean(skill.enabled)}
              onToggle={() => onToggle(skill)}
              pending={toggling}
            />
          ) : null}
          <Button
            disabled={!skill.skill_id}
            onClick={() => onEdit(skill)}
            size="sm"
            type="button"
          >
            编辑
          </Button>
        </div>
      </div>
      {skill.description ? (
        <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-neutral-500">
          {skill.description}
        </p>
      ) : null}
      {skill.latest_scan ? (
        <p className="mt-2 text-[11px] text-neutral-400">
          安全扫描：{skill.latest_scan.is_safe ? "通过" : "未通过"} · 最高{" "}
          {skill.latest_scan.max_severity} · {skill.latest_scan.findings_count} 个发现
        </p>
      ) : null}
      {skill.active ? (
        <p
          className={
            "mt-1 text-[11px] " + (skill.enabled ? "text-emerald-600" : "text-neutral-500")
          }
        >
          已生效 · {skill.enabled ? "已启用" : "已停用，不参与技能检索"}
          {skill.index_status ? " · " + skillIndexStatusLabel(skill.index_status) : ""}
        </p>
      ) : null}
    </li>
  );
}

/** 启用开关：轨道 30×16、滑块 12，开=绿色(emerald-500)、关=中性灰，180ms 过渡。
 *
 * 尺寸与配色对齐 DeepSeek Harness 技能中心：细轨 + 小滑块 + 极短动画，
 * 不再用「整体着色 + 大滑块」那种按钮化外观。
 */
function SkillEnabledSwitch({
  enabled,
  onToggle,
  pending,
}: {
  enabled: boolean;
  onToggle: () => void;
  pending: boolean;
}) {
  return (
    <button
      aria-checked={enabled}
      aria-label={enabled ? "停用技能" : "启用技能"}
      className="inline-flex shrink-0 cursor-pointer items-center rounded-full border-0 bg-transparent p-0.5 outline-none disabled:cursor-not-allowed disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-emerald-500/40"
      disabled={pending}
      onClick={onToggle}
      role="switch"
      title={enabled ? "已启用：参与技能检索" : "已停用：不参与技能检索"}
      type="button"
    >
      <span
        aria-hidden="true"
        className={
          "relative block h-4 w-[30px] shrink-0 rounded-full transition-colors duration-[180ms] " +
          (enabled ? "bg-emerald-500" : "bg-neutral-300")
        }
      >
        <span
          className={
            "absolute top-0.5 h-3 w-3 rounded-full bg-white shadow-[0_1px_2px_rgba(0,0,0,0.25)] transition-[left] duration-[180ms] " +
            (enabled ? "left-4" : "left-0.5")
          }
        />
      </span>
    </button>
  );
}

/** 会话提交后由 OpenViking 提炼、导入到私有草稿的技能。 */
function ExtractedTag() {
  return (
    <span className="inline-flex shrink-0 items-center rounded-full border border-violet-200 bg-violet-50 px-2 py-0.5 text-[11px] leading-none text-violet-700">
      提炼
    </span>
  );
}

/** 私有 / 内置。审核状态已移除：生命周期由 SkillStatusBadge 单独表达。 */
function ScopeTag({ scope }: { scope: SkillListItem["scope"] }) {
  const label = scope === "builtin" ? "内置" : "私有";
  const tone =
    scope === "builtin"
      ? "border-amber-200 bg-amber-50 text-amber-700"
      : "border-sky-200 bg-sky-50 text-sky-700";
  return (
    <span
      className={
        "inline-flex shrink-0 items-center rounded-full border px-2 py-0.5 text-[11px] leading-none " +
        tone
      }
    >
      {label}
    </span>
  );
}
