import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InterruptPanel } from "./interrupt-panel";

afterEach(cleanup);

const interrupt = {
  id: "i1",
  toolName: "update_user_profile",
  description: "模型准备更新你的「我的画像」。\n- 称呼：小林",
  allowedDecisions: ["approve", "reject"],
  actionCount: 1,
};

describe("InterruptPanel", () => {
  it("展示工具、变更说明和确认按钮", () => {
    const view = render(<InterruptPanel interrupt={interrupt} onDecide={() => {}} />);

    expect(view.getByRole("alertdialog", { name: "需要你确认" })).toBeTruthy();
    expect(view.getByText(/update_user_profile/)).toBeTruthy();
    expect(view.getByText(/称呼：小林/)).toBeTruthy();
    expect(view.getByRole("button", { name: "允许并继续" })).toBeTruthy();
    expect(view.getByRole("button", { name: "拒绝" })).toBeTruthy();
  });

  it("点击按钮把 approve / reject 交给上层", () => {
    const onDecide = vi.fn();
    const view = render(<InterruptPanel interrupt={interrupt} onDecide={onDecide} />);

    fireEvent.click(view.getByRole("button", { name: "允许并继续" }));
    fireEvent.click(view.getByRole("button", { name: "拒绝" }));

    expect(onDecide).toHaveBeenNthCalledWith(1, "approve");
    expect(onDecide).toHaveBeenNthCalledWith(2, "reject");
  });

  it("提交时禁用按钮", () => {
    const view = render(<InterruptPanel busy interrupt={interrupt} onDecide={() => {}} />);

    expect(view.getByRole("button", { name: "处理中…" })).toHaveProperty("disabled", true);
    expect(view.getByRole("button", { name: "拒绝" })).toHaveProperty("disabled", true);
  });
});
