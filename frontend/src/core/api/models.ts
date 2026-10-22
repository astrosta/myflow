import { apiFetch, apiUrl } from "./base";

/**
 * 模型目录：模型选择与推理等级的**唯一来源**是 config.yaml 的 models 段。
 *
 * 前端不维护任何模型清单、也不猜默认值：
 * - `default_model` 是服务端的缺省模型（agents.default_model，缺省时是 models[0]）；
 * - 每个模型自己公布 `reasoning_efforts`，没公布就是没有推理等级可选。
 */

export interface ReasoningEffort {
  /** 原样发给 provider 的取值（low / medium / high / minimal …） */
  id: string;
  label: string;
  description: string | null;
  /** 配置里标了 default 的那一档：composer 用它预选 */
  default: boolean;
}

export interface ModelInfo {
  name: string;
  model: string;
  display_name: string | null;
  description: string | null;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
  supports_vision: boolean;
  context_window: number | null;
  reasoning_efforts: ReasoningEffort[];
  default_reasoning_effort: string | null;
}

export interface ModelCatalog {
  models: ModelInfo[];
  default_model: string | null;
}

export async function listModels(options: { signal?: AbortSignal } = {}): Promise<ModelCatalog> {
  const response = await apiFetch(apiUrl("/models"), { signal: options.signal });
  if (!response.ok) {
    throw new Error("加载模型列表失败：HTTP " + response.status);
  }
  const body = (await response.json()) as Partial<ModelCatalog>;
  return {
    models: Array.isArray(body.models) ? body.models : [],
    default_model: typeof body.default_model === "string" ? body.default_model : null,
  };
}

export function modelLabel(model: ModelInfo | null | undefined): string {
  if (!model) return "";
  return model.display_name?.trim() || model.name;
}
