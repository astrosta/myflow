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

export function effortLabel(model: ModelInfo | null, effortId: string | null): string {
  if (!effortId) return "默认";
  return effortOptions(model).find((item) => item.id === effortId)?.label ?? effortId;
}

/**
 * 交给网关的装配上下文。**空值不下发**：网关会把 null 当成“显式选择”去校验，
 * 而“没选”本来就该走服务端默认。
 */
export function buildRunContext(selection: ModelSelection): Record<string, string> {
  const context: Record<string, string> = {};
  if (selection.modelName) context.model_name = selection.modelName;
  if (selection.effortId) context.reasoning_effort = selection.effortId;
  return context;
}
