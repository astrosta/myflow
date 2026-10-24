import { apiFetch, apiUrl } from "./base";

/**
 * 模型目录。**列表来自厂商**：网关会带着配置里的 key 去调 `GET {base_url}/models`，
 * 再把结果映射回 config.yaml 的条目（新模型用 `<name>@<vendor-id>`）。
 *
 * - `provider` 是厂商展示名：输入框里模型列表上方那行小字；
 * - `source` 区分"配置里写死的条目"和"厂商刚报出来的新模型"；
 * - `vendor_errors` 说明某些厂商没拉到（列表已回退到配置，不影响可用性）。
 */

export interface ReasoningEffort {
  /** 原样发给 provider 的取值（low / medium / high / minimal …） */
  id: string;
  label: string;
  description: string | null;
  /** 配置里标了 default（或厂商给了推荐值）的那一档：composer 用它预选 */
  default: boolean;
}

export interface ModelInfo {
  /** 回传给网关的标识：`models[].name` 或 `<name>@<vendor-id>` */
  name: string;
  /** 厂商侧模型 id */
  model: string;
  provider: string;
  display_name: string | null;
  description: string | null;
  supports_thinking: boolean;
  supports_reasoning_effort: boolean;
  supports_vision: boolean;
  context_window: number | null;
  reasoning_efforts: ReasoningEffort[];
  default_reasoning_effort: string | null;
  source: "config" | "vendor";
  available: boolean;
}

export interface ModelCatalog {
  models: ModelInfo[];
  default_model: string | null;
  default_reasoning_effort: string | null;
  vendor_errors: string[];
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
    default_reasoning_effort:
      typeof body.default_reasoning_effort === "string" ? body.default_reasoning_effort : null,
    vendor_errors: Array.isArray(body.vendor_errors) ? body.vendor_errors : [],
  };
}

/** 模型显示名**原样**用（display_name 或 name），不做大小写改造。 */
export function modelLabel(model: ModelInfo | null | undefined): string {
  if (!model) return "";
  return model.display_name?.trim() || model.name;
}
