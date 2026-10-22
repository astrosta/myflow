import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ModelInfo } from "@/core/api/models";

import { ModelPicker } from "./model-picker";

/**
 * 模型 / 推理等级选择器：目录完全由 props（也就是 `/api/models`）决定，
 * 所以这些用例断言的是"配置里有什么，菜单里就有什么"。
 */

afterEach(cleanup);

function model(overrides: Partial<ModelInfo> = {}): ModelInfo {
  return {
    name: "flash",
    model: "deepseek-v4-flash",
    display_name: null,
    description: null,
    supports_thinking: true,
    supports_reasoning_effort: true,
    supports_vision: false,
    context_window: null,
    reasoning_efforts: [
      { id: "low", label: "低", description: "快速响应", default: false },
      { id: "high", label: "高", description: "更慢更贵", default: false },
    ],
    default_reasoning_effort: null,
    ...overrides,
  };
}

function setup(props: Partial<React.ComponentProps<typeof ModelPicker>> = {}) {
  const onSelectModel = vi.fn();
  const onSelectEffort = vi.fn();
  const view = render(
    <ModelPicker
      current={model()}
      effortId="low"
      effortLabel="低"
      effortOptions={model().reasoning_efforts}
      models={[model()]}
      onSelectEffort={onSelectEffort}
      onSelectModel={onSelectModel}
      {...props}
    />,
  );
  return { view, onSelectModel, onSelectEffort };
}

describe("触发器", () => {
  it("一行显示当前模型与推理等级", () => {
    const { view } = setup();
    expect(view.getByRole("button", { name: /选择模型/ }).textContent).toContain("flash · 低");
  });

  it("模型没有公布等级时只显示模型名", () => {
    const plain = model({ name: "plain", supports_reasoning_effort: false, reasoning_efforts: [] });
    const { view } = setup({ current: plain, effortOptions: [], effortId: null, effortLabel: "默认" });
    expect(view.getByRole("button", { name: /选择模型/ }).textContent).toContain("plain");
  });
});

describe("菜单", () => {
  it("点开后有模型与推理等级两段", () => {
    const { view } = setup();
    fireEvent.click(view.getByRole("button", { name: /选择模型/ }));

    const menu = view.getByRole("menu", { name: "模型与推理等级" });
    expect(menu.textContent).toContain("模型");
    expect(menu.textContent).toContain("推理等级");
    expect(view.getAllByRole("menuitemradio").map((item) => item.textContent)).toEqual([
      "flash",
      "默认不指定，交给服务端与 provider",
      "低快速响应",
      "高更慢更贵",
    ]);
  });

  it("选中模型后回调的是配置里的 name，并收起菜单", () => {
    const other = model({ name: "pro", display_name: "Pro 版" });
    const { view, onSelectModel } = setup({ models: [model(), other] });
    fireEvent.click(view.getByRole("button", { name: /选择模型/ }));
    fireEvent.click(view.getByRole("menuitemradio", { name: /Pro 版/ }));

    expect(onSelectModel).toHaveBeenCalledWith("pro");
    expect(view.queryByRole("menu")).toBeNull();
  });

  it("选等级回调 id，选“默认”回调 null", () => {
    const { view, onSelectEffort } = setup();
    fireEvent.click(view.getByRole("button", { name: /选择模型/ }));
    fireEvent.click(view.getByRole("menuitemradio", { name: /高/ }));
    expect(onSelectEffort).toHaveBeenCalledWith("high");

    fireEvent.click(view.getByRole("button", { name: /选择模型/ }));
    fireEvent.click(view.getByRole("menuitemradio", { name: /默认/ }));
    expect(onSelectEffort).toHaveBeenLastCalledWith(null);
  });

  it("模型没有候选时不显示推理等级段，只留一句说明", () => {
    const plain = model({ reasoning_efforts: [] });
    const { view } = setup({ current: plain, effortOptions: [], effortId: null });
    fireEvent.click(view.getByRole("button", { name: /选择模型/ }));

    const menu = view.getByRole("menu");
    // 没有候选就没有"推理等级"那一段（只有那句说明，没有可点的选项）
    expect(menu.textContent).not.toContain("推理等级低");
    expect(view.queryAllByRole("menuitemradio").map((item) => item.textContent)).toEqual([
      "flash",
    ]);
    expect(menu.textContent).toContain("当前模型未提供推理等级");
  });

  it("Esc 关闭菜单", () => {
    const { view } = setup();
    fireEvent.click(view.getByRole("button", { name: /选择模型/ }));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(view.queryByRole("menu")).toBeNull();
  });

  it("目录为空时说清楚是配置里没有模型", () => {
    const { view } = setup({ models: [], current: null });
    fireEvent.click(view.getByRole("button", { name: /选择模型/ }));
    expect(view.getByRole("menu").textContent).toContain("配置里没有可用的模型");
  });
});
