"use client";

import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo, useState } from "react";

import { listModels, modelLabel, type ModelCatalog, type ModelInfo } from "../api/models";
import { loadSettings, saveSettings } from "../settings/store";
import {
  buildRunContext,
  effortLabel,
  effortOptions,
  resolveEffort,
  resolveModel,
  type ModelSelection,
} from "./selection";

/**
 * composer 的模型控制条：目录来自 `/api/models`，选择落到 localStorage。
 *
 * 为什么不把选择放进 thread 元数据：M5 §2 的约定是“界面偏好放本地”，
 * 而且换会话后再点开新对话，用户通常想沿用上次的选择。
 */
export interface ModelSelectionController {
  models: ModelInfo[];
  current: ModelInfo | null;
  currentLabel: string;
  effortOptions: ReturnType<typeof effortOptions>;
  effortId: string | null;
  effortLabel: string;
  selection: ModelSelection;
  runContext: Record<string, string>;
  loading: boolean;
  error: string | null;
  selectModel: (name: string) => void;
  selectEffort: (id: string | null) => void;
}

export function useModelSelection(): ModelSelectionController {
  const [stored, setStored] = useState(() => loadSettings());

  const query = useQuery<ModelCatalog>({
    queryKey: ["models"],
    queryFn: ({ signal }) => listModels({ signal }),
    // 模型目录是启动时读的配置：不主动刷新，切回页面时顺手拉一次就够
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
    retry: 1,
  });

  const current = useMemo(
    () => resolveModel(query.data, stored.modelName),
    [query.data, stored.modelName],
  );

  const effortId = useMemo(
    () => resolveEffort(current, stored.reasoningEffort),
    [current, stored.reasoningEffort],
  );

  const selectModel = useCallback((name: string) => {
    // 换模型时等级要重新协商：新模型的候选和旧模型不一定重合（见 selection.ts）
    setStored(saveSettings({ modelName: name, reasoningEffort: null }));
  }, []);

  const selectEffort = useCallback((id: string | null) => {
    setStored(saveSettings({ reasoningEffort: id }));
  }, []);

  const selection = useMemo<ModelSelection>(
    () => ({ modelName: current?.name ?? null, effortId }),
    [current?.name, effortId],
  );

  return {
    models: query.data?.models ?? [],
    current,
    currentLabel: modelLabel(current),
    effortOptions: effortOptions(current),
    effortId,
    effortLabel: effortLabel(current, effortId),
    selection,
    runContext: buildRunContext(selection),
    loading: query.isLoading,
    error: query.error instanceof Error ? query.error.message : null,
    selectModel,
    selectEffort,
  };
}
