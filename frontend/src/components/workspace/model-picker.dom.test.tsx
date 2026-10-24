import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ModelInfo } from "@/core/api/models";

import { ModelPicker } from "./model-picker";

/**
 * 三层上拉框：根菜单（模型 / 推理等级两行）→ 模型列表（厂商小字 + 打勾）
 * 或推理等级列表（Off + 候选 + 打勾）。候选全部来自 props（`/api/models`）。
 */

afterEach(cleanup);

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
    reasoning_efforts: [
      { id: "low", label: "Low", description: "这条描述不该显示", default: false },
      { id: "high", label: "High", description: null, default: false },
    ],
    default_reasoning_effort: null,
    source: "vendor",
    available: true,
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
      effortLabel="Low"
      effortOptions={model().reasoning_efforts}
      models={[model()]}
      onSelectEffort={onSelectEffort}
      onSelectModel={onSelectModel}
      {...props}
    />,
  );
  return { view, onSelectModel, onSelectEffort };
}

const trigger = (view: ReturnType<typeof render>) =>
  view.getByRole("button", { name: /选择模型/ });

describe("触发器", () => {
  it("显示模型名（原样）与当前推理等级", () => {
    const { view } = setup();
    expect(trigger(view).textContent).toContain("flash · Low");
  });

  it("没选等级时显示 Off", () => {
    const { view } = setup({ effortId: null, effortLabel: "Off" });
    expect(trigger(view).textContent).toContain("flash · Off");
  });

  it("模型没有公布等级时只显示模型名", () => {
    const plain = model({ name: "plain", supports_reasoning_effort: false, reasoning_efforts: [] });
    const { view } = setup({ current: plain, effortOptions: [], effortId: null, effortLabel: "Off" });
    expect(trigger(view).textContent).toContain("plain");
  });
});

describe("根菜单", () => {
  it("点开先是模型 / 推理等级两行，各显示当前值", () => {
    const { view } = setup();
    fireEvent.click(trigger(view));

    const menu = view.getByRole("menu", { name: "模型与推理等级" });
    expect(menu.textContent).toContain("模型");
    expect(menu.textContent).toContain("flash");
    expect(menu.textContent).toContain("推理等级");
    expect(menu.textContent).toContain("Low");
  });

  it("模型没有候选时不显示推理等级那一行", () => {
    const plain = model({ reasoning_efforts: [] });
    const { view } = setup({ current: plain, effortOptions: [], effortId: null });
    fireEvent.click(trigger(view));

    const menu = view.getByRole("menu");
    expect(menu.textContent).toContain("模型");
    expect(view.queryByRole("menuitem", { name: "推理等级" })).toBeNull();
  });
});

describe("模型列表", () => {
  it("点“模型”进入列表：厂商名是小字，当前模型后面打勾", () => {
    const { view } = setup({ models: [model(), model({ name: "pro", display_name: "pro" })] });
    fireEvent.click(trigger(view));
    fireEvent.click(view.getByRole("menuitem", { name: "模型" }));

    const menu = view.getByRole("menu", { name: "模型列表" });
    expect(menu.textContent).toContain("DeepSeek");

    const rows = view.getAllByRole("menuitemradio");
    expect(rows[0]?.getAttribute("aria-checked")).toBe("true");
    expect(rows[0]?.textContent).toContain("✓");
    // 原样显示 display_name
    expect(rows[1]?.textContent).toContain("pro");
    expect(rows[1]?.getAttribute("aria-checked")).toBe("false");
  });

  it("多厂商按厂商分组", () => {
    const { view } = setup({
      models: [model(), model({ name: "gpt5", model: "gpt-5", provider: "OpenAI" })],
    });
    fireEvent.click(trigger(view));
    fireEvent.click(view.getByRole("menuitem", { name: "模型" }));

    const menu = view.getByRole("menu");
    expect(menu.textContent).toContain("DeepSeek");
    expect(menu.textContent).toContain("OpenAI");
  });

  it("点模型就是选中它，并收起菜单", () => {
    const other = model({ name: "pro", display_name: "pro" });
    const { view, onSelectModel } = setup({ models: [model(), other] });
    fireEvent.click(trigger(view));
    fireEvent.click(view.getByRole("menuitem", { name: "模型" }));
    fireEvent.click(view.getByRole("menuitemradio", { name: /^pro$/ }));

    expect(onSelectModel).toHaveBeenCalledWith("pro");
    expect(view.queryByRole("menu")).toBeNull();
  });

  it("列表里只显示名称，不显示描述", () => {
    const { view } = setup({
      models: [model({ description: "这是一段不该出现的中文描述" })],
    });
    fireEvent.click(trigger(view));
    fireEvent.click(view.getByRole("menuitem", { name: "模型" }));

    expect(view.getByRole("menu").textContent).not.toContain("不该出现");
  });

  it("目录为空时说清楚没有可用模型", () => {
    const { view } = setup({ models: [], current: null });
    fireEvent.click(trigger(view));
    fireEvent.click(view.getByRole("menuitem", { name: "模型" }));

    expect(view.getByRole("menu").textContent).toContain("配置里没有可用的模型");
  });

  it("厂商列表没拉到时有提示，模型仍可选", () => {
    const { view } = setup({ vendorErrors: ["OpenAI: RuntimeError"] });
    fireEvent.click(trigger(view));
    fireEvent.click(view.getByRole("menuitem", { name: "模型" }));

    expect(view.getByRole("menu").textContent).toContain("部分厂商列表不可用");
    expect(view.getAllByRole("menuitemradio").length).toBe(1);
  });
});

describe("推理等级列表", () => {
  it("Off 在最前面，当前等级后面打勾，且不显示描述", () => {
    const { view } = setup();
    fireEvent.click(trigger(view));
    fireEvent.click(view.getByRole("menuitem", { name: "推理等级" }));

    const menu = view.getByRole("menu", { name: "推理等级列表" });
    const rows = view.getAllByRole("menuitemradio");
    expect(rows.map((row) => row.textContent)).toEqual(["Off", "Low✓", "High"]);
    expect(menu.textContent).not.toContain("这条描述不该显示");
  });

  it("没选等级时 Off 打勾", () => {
    const { view } = setup({ effortId: null, effortLabel: "Off" });
    fireEvent.click(trigger(view));
    fireEvent.click(view.getByRole("menuitem", { name: "推理等级" }));

    const rows = view.getAllByRole("menuitemradio");
    expect(rows[0]?.getAttribute("aria-checked")).toBe("true");
    expect(rows[0]?.textContent).toContain("✓");
    expect(rows[1]?.getAttribute("aria-checked")).toBe("false");
  });

  it("选等级回调 id，选 Off 回调 null，都收起菜单", () => {
    const { view, onSelectEffort } = setup();
    fireEvent.click(trigger(view));
    fireEvent.click(view.getByRole("menuitem", { name: "推理等级" }));
    fireEvent.click(view.getByRole("menuitemradio", { name: "High" }));
    expect(onSelectEffort).toHaveBeenCalledWith("high");
    expect(view.queryByRole("menu")).toBeNull();

    fireEvent.click(trigger(view));
    fireEvent.click(view.getByRole("menuitem", { name: "推理等级" }));
    fireEvent.click(view.getByRole("menuitemradio", { name: "Off" }));
    expect(onSelectEffort).toHaveBeenLastCalledWith(null);
  });
});

describe("层级导航", () => {
  it("列表页能退回根菜单", () => {
    const { view } = setup();
    fireEvent.click(trigger(view));
    fireEvent.click(view.getByRole("menuitem", { name: "模型" }));
    fireEvent.click(view.getByRole("button", { name: "返回模型" }));

    expect(view.getByRole("menu", { name: "模型与推理等级" })).toBeTruthy();
  });

  it("推理等级列表也能退回根菜单", () => {
    const { view } = setup();
    fireEvent.click(trigger(view));
    fireEvent.click(view.getByRole("menuitem", { name: "推理等级" }));
    fireEvent.click(view.getByRole("button", { name: "返回推理等级" }));

    expect(view.getByRole("menu", { name: "模型与推理等级" })).toBeTruthy();
  });

  it("Esc 关闭，再次打开回到根菜单", () => {
    const { view } = setup();
    fireEvent.click(trigger(view));
    fireEvent.click(view.getByRole("menuitem", { name: "模型" }));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(view.queryByRole("menu")).toBeNull();

    fireEvent.click(trigger(view));
    expect(view.getByRole("menu", { name: "模型与推理等级" })).toBeTruthy();
  });
});
