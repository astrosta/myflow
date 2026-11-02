"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import { BookOpenIcon } from "@/components/ui/icons";
import { Skeleton } from "@/components/ui/skeleton";
import {
  listMySkills,
  listSkills,
  openDraft,
  SkillApiError,
  type SkillListItem,
} from "@/core/api/skills";
import { useSession } from "@/core/auth/use-session";

import { CreateSkillDialog } from "./create-skill-dialog";
import { SkillStatusBadge } from "./skill-status-badge";

/**
 * 「我的技能」：左半是**我自己的私有草稿**（/skills/mine，作者路由），
 * 右半是**已发布的公共目录**（/skills?scope=public）：内置技能单独只读列出，
 * 公共行只读 + 自己的那一行可回编辑器。
 *
 * 两个列表必须分开取：mine 是 author 路由，行主键叫 id、永远 scope=private；
 * 公共目录是运行时快照，行是 skill_id、来源可能是 filesystem（skill_id
 * 为空时客户端回退到 name）。scope=public 还能避免作者自己的私有副本把同名
 * 公共行压掉；否则审核中的草稿会混进公共目录，已发布版本也会对作者隐身。
 *
 * 页面只负责装配：取数、空态、错误、创建后失效与跳转。
 */

/** 接口错误给一句人读的话：网关的 detail 最具体，没有就退回 status。 */
function readableError(error: unknown): string {
  if (error instanceof SkillApiError) {
    return error.detail || "请求失败（HTTP " + error.status + "）";
  }
  if (error instanceof Error) return error.message;
  return String(error);
}

const editorHref = (skillId: string) => "/workspace/skills/" + encodeURIComponent(skillId);

const ROW_CLASS = "rounded-lg border border-neutral-200 bg-white p-4 transition-colors";

/**
 * 公共目录。新网关用 ?scope=public 直接拿已发布行；还没重启的旧网关会
 * 把 scope 判成 422，此时退回 effective 并在客户端剔除草稿。
 *
 * 回退分支只认 approved 的私有行，因为旧快照里作者自己的公共版本会被
 * 同名私有行压掉；这不是新的权威来源，只是为了滚动升级期间页面能打开。
 */
async function fetchPublicCatalog(): Promise<SkillListItem[]> {
  try {
    return await listSkills("public");
  } catch (cause) {
    if (
      !(cause instanceof SkillApiError) ||
      cause.status !== 422 ||
      !cause.detail.includes("query.scope")
    ) {
      throw cause;
    }
    const rows = await listSkills();
    return rows.reduce<SkillListItem[]>((visible, skill) => {
      if (skill.scope === "public" || skill.scope === "builtin") {
        visible.push(skill);
      } else if (skill.scope === "private" && skill.status === "approved") {
        visible.push({ ...skill, scope: "public" });
      }
      return visible;
    }, []);
  }
}

export function MySkillsPage() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { user } = useSession();
  const [creating, setCreating] = useState(false);
  const [openingId, setOpeningId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const mineQuery = useQuery({
    queryKey: ["skills", "mine"],
    queryFn: listMySkills,
    staleTime: 5_000,
  });
  const catalogQuery = useQuery({
    queryKey: ["skills", "public"],
    queryFn: fetchPublicCatalog,
    staleTime: 5_000,
  });

  const mine = mineQuery.data ?? [];
  const catalog = catalogQuery.data ?? [];
  // 公共目录只接收已发布行；内置技能仍从同一快照里单列。
  const builtins = catalog.filter((skill) => skill.scope === "builtin");
  const publicSkills = catalog.filter((skill) => skill.scope === "public");
  // 已发布版本在 /skills/mine 里仍是一条私有记录。只有它的版本和公共目录
  // 当前发布版本一致时才从草稿区隐藏；作者已经开始改下一版时仍留在草稿区。
  const publishedVersionIds = new Set(
    publicSkills
      .map((skill) => skill.version_id)
      .filter((versionId): versionId is string => versionId !== null),
  );
  const drafts = mine.filter((skill) => {
    if (skill.status !== "approved" || skill.version_id === null) return true;
    return !publishedVersionIds.has(skill.version_id);
  });
  const error = mineQuery.error ?? catalogQuery.error;
  const loading = mineQuery.isLoading || catalogQuery.isLoading;

  const openEditor = (skill: SkillListItem) => {
    router.push(editorHref(skill.skill_id));
  };

  const created = (skill: SkillListItem) => {
    setCreating(false);
    void queryClient.invalidateQueries({ queryKey: ["skills", "mine"] });
    openEditor(skill);
  };

  /**
   * 公共技能的「编辑」＝先开一份草稿（后端把公共版本复制进私有草稿），
   * 再进编辑器。openDraft 失败就停在原地报错，不要跳到一个打不开的页面。
   */
  const editOwnPublic = async (skill: SkillListItem) => {
    if (openingId) return;
    setOpeningId(skill.skill_id);
    setActionError(null);
    try {
      await openDraft(skill.skill_id);
      void queryClient.invalidateQueries({ queryKey: ["skills", "mine"] });
      openEditor(skill);
    } catch (cause) {
      setActionError(readableError(cause));
    } finally {
      setOpeningId(null);
    }
  };

  const empty = !loading && !error && mine.length === 0 && catalog.length === 0;

  return (
    <div className="h-full overflow-y-auto bg-white">
      <div className="mx-auto w-full max-w-5xl px-5 py-6 sm:px-8">
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-neutral-200 pb-5">
          <div>
            <div className="flex items-center gap-2">
              <BookOpenIcon className="text-neutral-500" size={18} />
              <h1 className="text-xl font-semibold tracking-tight text-neutral-900">技能</h1>
            </div>
            <p className="mt-1 text-xs leading-relaxed text-neutral-500">
              私有草稿只有你自己能看到；提交审核并通过后才会进入公共目录。
            </p>
          </div>
          <div className="flex gap-2">
            <Button
              disabled={mineQuery.isFetching || catalogQuery.isFetching}
              onClick={() => {
                void mineQuery.refetch();
                void catalogQuery.refetch();
              }}
              size="sm"
              type="button"
            >
              刷新
            </Button>
            <Button
              onClick={() => setCreating(true)}
              size="sm"
              type="button"
              variant="primary"
            >
              ＋ 新建技能
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

        {actionError ? (
          <p
            className="mt-5 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700"
            role="alert"
          >
            {actionError}
          </p>
        ) : null}

        {empty ? (
          <div className="mt-8 rounded-lg border border-dashed border-neutral-300 bg-neutral-50/60 px-6 py-12 text-center">
            <BookOpenIcon className="mx-auto text-neutral-300" size={28} />
            <p className="mt-3 text-sm font-medium text-neutral-700">还没有技能</p>
            <p className="mt-1 text-xs text-neutral-500">
              新建一个草稿，写好 SKILL.md 后提交审核；通过后团队里的所有人都能用。
            </p>
            <Button
              className="mt-4"
              onClick={() => setCreating(true)}
              size="sm"
              type="button"
              variant="primary"
            >
              新建技能
            </Button>
          </div>
        ) : null}

        {drafts.length > 0 ? (
          <section className="mt-6">
            <h2 className="text-xs font-medium text-neutral-500">我的草稿</h2>
            <ul className="mt-3 grid gap-3 sm:grid-cols-2">
              {drafts.map((skill) => (
                <li className={ROW_CLASS} key={skill.skill_id || skill.name}>
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <h3 className="truncate text-sm font-semibold text-neutral-900">
                        {skill.name || "未命名技能"}
                      </h3>
                      <div className="mt-1 flex flex-wrap items-center gap-1.5">
                        <ScopeTag scope={skill.scope} />
                        <SkillStatusBadge status={skill.status} />
                      </div>
                    </div>
                    <SkillEditButton
                      disabled={openingId !== null}
                      onClick={() => openEditor(skill)}
                    />
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

        {publicSkills.length > 0 ? (
          <section className="mt-6">
            <h2 className="text-xs font-medium text-neutral-500">公共技能</h2>
            <ul className="mt-3 grid gap-3 sm:grid-cols-2">
              {publicSkills.map((skill) => {
                const owned = Boolean(user?.id) && skill.owner_user_id === user?.id;
                return (
                  <li className={ROW_CLASS} key={skill.skill_id || skill.name}>
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <h3 className="truncate text-sm font-semibold text-neutral-900">
                          {skill.name || "未命名技能"}
                        </h3>
                        <div className="mt-1 flex flex-wrap items-center gap-1.5">
                          <ScopeTag scope={skill.scope} />
                          <SkillStatusBadge status={skill.status} />
                          {skill.skill_version ? (
                            <span className="text-[11px] text-neutral-400">
                              v{skill.skill_version}
                            </span>
                          ) : null}
                        </div>
                      </div>
                      {owned ? (
                        <SkillEditButton
                          disabled={openingId !== null}
                          onClick={() => void editOwnPublic(skill)}
                        />
                      ) : null}
                    </div>
                    {skill.description ? (
                      <p className="mt-2 line-clamp-2 text-xs leading-relaxed text-neutral-500">
                        {skill.description}
                      </p>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          </section>
        ) : null}

        <p className="mt-8 text-[11px] text-neutral-400">
          技能按用户隔离编辑；公共目录只展示已通过审核的版本。
        </p>
      </div>

      {creating ? <CreateSkillDialog onClose={() => setCreating(false)} onCreated={created} /> : null}
    </div>
  );
}

/** 草稿和公共技能共用同一颗编辑按钮，避免两个区块的卡片各长一样。 */
function SkillEditButton({ disabled, onClick }: { disabled: boolean; onClick: () => void }) {
  return (
    <Button disabled={disabled} onClick={onClick} size="sm" type="button">
      编辑
    </Button>
  );
}

/** 私有/公共/内置。和状态徽标分开：一个说"谁能看"，一个说"审核到哪了"。 */
function ScopeTag({ scope }: { scope: SkillListItem["scope"] }) {
  const label = scope === "private" ? "私有" : scope === "builtin" ? "内置" : "公共";
  const tone =
    scope === "private"
      ? "border-sky-200 bg-sky-50 text-sky-700"
      : scope === "builtin"
        ? "border-amber-200 bg-amber-50 text-amber-700"
        : "border-violet-200 bg-violet-50 text-violet-700";
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
