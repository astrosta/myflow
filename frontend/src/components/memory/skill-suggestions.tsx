"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { Button } from "@/components/ui/button";
import {
  confirmSkillCandidate,
  listSkillCandidates,
  MemoryApiError,
  rejectSkillCandidate,
  type SkillCandidate,
} from "@/core/api/memory";

function readableError(error: unknown): string {
  if (error instanceof MemoryApiError) {
    if (error.status === 409) return "候选已更新或包校验失败，请刷新后再试。";
    return error.detail || "请求失败（HTTP " + error.status + "）";
  }
  return error instanceof Error ? error.message : String(error);
}

function idempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return "memory-skill-" + Date.now();
}

export function SkillSuggestions() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ["memory", "skill-candidates"],
    queryFn: listSkillCandidates,
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["memory", "skill-candidates"] });
  };

  const confirm = useMutation({
    mutationFn: (candidate: SkillCandidate) =>
      confirmSkillCandidate(candidate.id, {
        expected_revision: candidate.revision,
        package_sha256: candidate.package_sha256,
        idempotency_key: idempotencyKey(),
      }),
    onSuccess: (result) => {
      setError(null);
      refresh();
      router.push("/workspace/skills/" + encodeURIComponent(result.skill_id));
    },
    onError: (cause) => setError(readableError(cause)),
  });

  const reject = useMutation({
    mutationFn: (candidate: SkillCandidate) =>
      rejectSkillCandidate(candidate.id, candidate.revision, "用户拒绝"),
    onSuccess: () => {
      setError(null);
      refresh();
    },
    onError: (cause) => setError(readableError(cause)),
  });

  const candidates = (query.data ?? []).filter(
    (candidate) => candidate.status === "pending_confirmation",
  );
  if (query.isLoading || candidates.length === 0) return null;

  return (
    <section className="mt-6">
      <div className="flex items-baseline justify-between gap-3">
        <h2 className="text-xs font-medium text-neutral-500">记忆建议</h2>
        <p className="text-[11px] text-neutral-400">确认后创建私有技能，公开仍需审核</p>
      </div>
      {error ? (
        <p className="mt-3 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-700" role="alert">
          {error}
        </p>
      ) : null}
      <ul className="mt-3 grid gap-3 sm:grid-cols-2">
        {candidates.map((candidate) => (
          <li className="rounded-lg border border-sky-200 bg-sky-50/40 p-4" key={candidate.id}>
            <h3 className="text-sm font-semibold text-neutral-900">{candidate.private_name}</h3>
            <p className="mt-1 text-xs leading-relaxed text-neutral-500">
              {candidate.description || "从成功情景中提取的可复用流程"}
            </p>
            <p className="mt-2 text-[11px] text-neutral-400">
              来源情景 {candidate.source_episode_ids.length} 条
            </p>
            <div className="mt-3 flex gap-2">
              <Button
                disabled={busy !== null}
                onClick={() => {
                  setBusy(candidate.id);
                  confirm.mutate(candidate, { onSettled: () => setBusy(null) });
                }}
                size="sm"
                type="button"
                variant="primary"
              >
                创建私有技能
              </Button>
              <Button
                disabled={busy !== null}
                onClick={() => {
                  setBusy(candidate.id);
                  reject.mutate(candidate, { onSettled: () => setBusy(null) });
                }}
                size="sm"
                type="button"
                variant="danger"
              >
                忽略
              </Button>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}
