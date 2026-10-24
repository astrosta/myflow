import { describe, expect, it } from "vitest";

import type { ModelCatalog, ModelInfo } from "../api/models";
import { loadSettings, parseSettings, saveSettings, type StorageLike } from "../settings/store";
import {
  buildRunContext,
  effortLabel,
  groupByProvider,
  resolveEffort,
  resolveModel,
} from "./selection";

function model(overrides: Partial<ModelInfo> = {}): ModelInfo {
  return {
    name: "flash",
    model: "deepseek-v4-flash",
    provider: "DeepSeek",
    display_name: null,
    description: null,
    supports_thinking: true,
    supports_reasoning_effort: true,
    supports_vision: false,
    context_window: null,
    reasoning_efforts: [],
    default_reasoning_effort: null,
    source: "vendor",
    available: true,
    ...overrides,
  };
}

const catalog: ModelCatalog = {
  default_model: "flash",
  default_reasoning_effort: null,
  vendor_errors: [],
  models: [
    model({
      reasoning_efforts: [
        { id: "low", label: "Low", description: null, default: false },
        { id: "high", label: "High", description: null, default: false },
      ],
    }),
    model({ name: "plain", supports_reasoning_effort: false }),
  ],
};

describe("模型回退", () => {
  it("本地存的名字还能解析就用它", () => {
    expect(resolveModel(catalog, "plain")?.name).toBe("plain");
  });

  it("存的模型已下线时回退到服务端默认，而不是把不存在的名字发出去", () => {
    expect(resolveModel(catalog, "ghost")?.name).toBe("flash");
  });

  it("没有默认模型时用第一条", () => {
    const withoutDefault: ModelCatalog = {
      models: catalog.models,
      default_model: null,
      default_reasoning_effort: null,
      vendor_errors: [],
    };
    expect(resolveModel(withoutDefault, null)?.name).toBe("flash");
  });

  it("目录还没加载时返回 null", () => {
    expect(resolveModel(undefined, "flash")).toBeNull();
  });
});

describe("按厂商分组", () => {
  it("同厂商的模型归一组，顺序按目录", () => {
    const groups = groupByProvider([
      model({ name: "a", provider: "DeepSeek" }),
      model({ name: "b", provider: "OpenAI" }),
      model({ name: "c", provider: "DeepSeek" }),
    ]);
    expect(groups.map((group) => group.provider)).toEqual(["DeepSeek", "OpenAI"]);
    expect(groups[0]?.models.map((item) => item.name)).toEqual(["a", "c"]);
  });

  it("没有厂商名时归到“其他”，不会出现空标题", () => {
    const groups = groupByProvider([model({ provider: "" })]);
    expect(groups[0]?.provider).toBe("其他");
  });
});

describe("推理等级", () => {
  it("模型没公布候选时永远是 null", () => {
    const plain = catalog.models[1]!;
    expect(resolveEffort(plain, "high")).toBeNull();
    expect(effortLabel(plain, null)).toBe("Off");
  });

  it("换模型后旧等级失效，回退到该模型自己的默认", () => {
    expect(resolveEffort(catalog.models[0]!, "medium")).toBeNull();
    expect(
      resolveEffort(
        model({
          default_reasoning_effort: "low",
          reasoning_efforts: catalog.models[0]!.reasoning_efforts,
        }),
        "medium",
      ),
    ).toBe("low");
  });

  it("存着的等级仍然有效就保留", () => {
    expect(resolveEffort(catalog.models[0]!, "high")).toBe("high");
    expect(effortLabel(catalog.models[0]!, "high")).toBe("High");
  });
});

describe("装配上下文", () => {
  it("不选就不下发：空字符串不会出现在 context 里", () => {
    expect(buildRunContext({ modelName: null, effortId: null })).toEqual({});
    expect(buildRunContext({ modelName: "flash", effortId: null })).toEqual({
      model_name: "flash",
    });
    expect(buildRunContext({ modelName: "flash", effortId: "high" })).toEqual({
      model_name: "flash",
      reasoning_effort: "high",
    });
  });

  it("模型支持思考时下发 thinking_enabled，网关按 bool 校验", () => {
    expect(buildRunContext({ modelName: "flash", effortId: null, thinkingEnabled: true })).toEqual({
      model_name: "flash",
      thinking_enabled: true,
    });
    expect(
      buildRunContext({ modelName: "flash", effortId: null, thinkingEnabled: false }),
    ).toEqual({ model_name: "flash" });
  });
});

describe("设置持久化", () => {
  function memoryStorage(): StorageLike & { data: Map<string, string> } {
    const data = new Map<string, string>();
    return {
      data,
      getItem: (key) => data.get(key) ?? null,
      setItem: (key, value) => void data.set(key, value),
      removeItem: (key) => void data.delete(key),
    };
  }

  it("旧版本（没有 reasoningEffort 字段）读出来不报错", () => {
    const parsed = parseSettings(JSON.stringify({ modelName: "flash", thinkingEnabled: false }));
    expect(parsed.modelName).toBe("flash");
    expect(parsed.reasoningEffort).toBeNull();
  });

  it("选择会写回本地并保留其它字段", () => {
    const storage = memoryStorage();
    saveSettings({ modelName: "flash" }, storage);
    saveSettings({ reasoningEffort: "high" }, storage);
    expect(loadSettings(storage)).toEqual({
      modelName: "flash",
      reasoningEffort: "high",
      thinkingEnabled: true,
    });
  });

  it("坏 JSON 回落到默认值", () => {
    expect(parseSettings("{not json")).toEqual({
      modelName: null,
      reasoningEffort: null,
      thinkingEnabled: true,
    });
  });
});
