"use client";

import { useSyncExternalStore } from "react";
import type { QueryClient } from "@tanstack/react-query";

import { scanSkill, type SecurityScan } from "@/core/api/skills";

/**
 * One in-flight security scan per skill, kept outside the editor component.
 *
 * The scan is a long request. Route navigation unmounts the editor, but the
 * fetch keeps running; local `useState` would forget the progress and the
 * returning page would look idle (and allow a duplicate scan). This small
 * external store keeps the run visible for the lifetime of the browser
 * session and refreshes the draft query when the scan settles.
 */

export type SkillScanRunStatus = "idle" | "running" | "failed";

export interface SkillScanRun {
  status: SkillScanRunStatus;
  /** Latest scanner phase message, or null before the first event. */
  message: string | null;
  /** Failure reason; only set when status is "failed". */
  error: unknown;
}

const IDLE: SkillScanRun = Object.freeze({
  status: "idle",
  message: null,
  error: null,
});

const runs = new Map<string, SkillScanRun>();
const listeners = new Map<string, Set<() => void>>();
const inflight = new Map<string, Promise<SecurityScan>>();

function emit(skillId: string): void {
  for (const listener of listeners.get(skillId) ?? []) listener();
}

function setRun(skillId: string, run: SkillScanRun): void {
  runs.set(skillId, run);
  emit(skillId);
}

/** Drop a terminal state before the next user action starts fresh. */
export function clearSkillScanRun(skillId: string): void {
  if (!runs.has(skillId) || inflight.has(skillId)) return;
  runs.delete(skillId);
  emit(skillId);
}

export function getSkillScanRun(skillId: string): SkillScanRun {
  return runs.get(skillId) ?? IDLE;
}

function subscribe(skillId: string, listener: () => void): () => void {
  const bucket = listeners.get(skillId) ?? new Set<() => void>();
  bucket.add(listener);
  listeners.set(skillId, bucket);
  return () => {
    bucket.delete(listener);
    if (bucket.size === 0) listeners.delete(skillId);
  };
}

export function useSkillScanRun(skillId: string): SkillScanRun {
  return useSyncExternalStore(
    (listener) => subscribe(skillId, listener),
    () => getSkillScanRun(skillId),
    () => IDLE,
  );
}

/**
 * Start (or join) the scan for one skill.
 *
 * The promise is shared by every caller so a remount cannot launch a second
 * scan. On success the draft, list and catalog queries are invalidated even
 * when the editor is unmounted, so returning to the page reads the persisted
 * scan result instead of a stale pre-scan draft.
 */
export function startSkillScanRun(
  skillId: string,
  queryClient: QueryClient,
): Promise<SecurityScan> {
  const existing = inflight.get(skillId);
  if (existing) return existing;

  setRun(skillId, { status: "running", message: null, error: null });
  const promise = (async () => {
    try {
      const result = await scanSkill(skillId, (message) => {
        const current = runs.get(skillId);
        if (current?.status !== "running") return;
        setRun(skillId, { ...current, message });
      });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["skills", "draft", skillId] }),
        queryClient.invalidateQueries({ queryKey: ["skills", "mine"] }),
        queryClient.invalidateQueries({ queryKey: ["skills", "catalog"] }),
      ]);
      runs.delete(skillId);
      emit(skillId);
      return result;
    } catch (error) {
      setRun(skillId, { status: "failed", message: null, error });
      throw error;
    } finally {
      inflight.delete(skillId);
    }
  })();

  inflight.set(skillId, promise);
  return promise;
}
