import type { ModelCatalog, ModelInfo, ReasoningEffort } from "../api/models";

/**
 * 模型 / 推理等级的选择逻辑（纯函数，可单测）。
 *
 * 三条不变量：
 * 1. **选择只落在目录里的行上**。本地存着上一个已下线模型的 id 时回退到服务端默认，
 *    而不是把一个 404 的模型名发出去。
 * 2. **等级必须属于当前模型**。换模型后旧等级失效，回退到“不指定”。
 * 3. null 表示“不指定”：不下发 model_name / reasoning_effort，走服务端默认。
 */

export interface ModelSelection {
  modelName: string | null;
  effortId: string | null;
  /** 是否让模型输出思考过程；必须由用户显式打开，模型支持只是前提 */
  thinkingEnabled?: boolean;
}

export function resolveModel(
  catalog: ModelCatalog | undefined,
  storedName: string | null,
): ModelInfo | null {
  const models = catalog?.models ?? [];
  if (!models.length) return null;
  const stored = storedName ? models.find((item) => item.name === storedName) : undefined;
  if (stored) return stored;
  const fallback = catalog?.default_model
    ? models.find((item) => item.name === catalog.default_model)
    : undefined;
  return fallback ?? models[0] ?? null;
}

export function effortOptions(model: ModelInfo | null): ReasoningEffort[] {
  return model?.reasoning_efforts?.length ? model.reasoning_efforts : [];
}

export function resolveEffort(model: ModelInfo | null, storedId: string | null): string | null {
  const options = effortOptions(model);
  if (!options.length) return null;
  if (storedId && options.some((item) => item.id === storedId)) return storedId;
  return model?.default_reasoning_effort ?? null;
}

export interface ProviderGroup {
  provider: string;
  models: ModelInfo[];
}

/** 按厂商分组：上拉框先显示厂商小字，再列这一家的模型（多厂商时不会混在一起）。 */
export function groupByProvider(models: ModelInfo[]): ProviderGroup[] {
  const groups: ProviderGroup[] = [];
  for (const model of models) {
    const provider = model.provider?.trim() || "其他";
    const existing = groups.find((group) => group.provider === provider);
    if (existing) existing.models.push(model);
    else groups.push({ provider, models: [model] });
  }
  return groups;
}

/** Off = 不指定等级（交给服务端与 provider 的默认值）；其余等级名原样显示。 */
export function effortLabel(model: ModelInfo | null, effortId: string | null): string {
  if (!effortId) return "Off";
  return effortOptions(model).find((item) => item.id === effortId)?.label ?? effortId;
}

export type RunContext = Record<string, string | boolean>;

/**
 * 交给网关的装配上下文。**空值不下发**：网关会把 null 当成“显式选择”去校验，
 * 而“没选”本来就该走服务端默认。
 *
 * `thinking_enabled` 是 bool（网关按类型校验），只有模型支持思考时才下发——
 * 不开就是真的不要思考内容，前端也不该假装有。
 */
export function buildRunContext(selection: ModelSelection): RunContext {
  const context: RunContext = {};
  if (selection.modelName) context.model_name = selection.modelName;
  if (selection.effortId) context.reasoning_effort = selection.effortId;
  if (selection.thinkingEnabled) context.thinking_enabled = true;
  return context;
}
