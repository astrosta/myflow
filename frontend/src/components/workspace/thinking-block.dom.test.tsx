import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ThinkingBlock } from "./thinking-block";

/**
 * 思考块：一行 `Think · 摘要`，流式时摘要跟着动（取最后一行），点开看全文。
 * 形制参考 DeepSeek Harness 的 ReasoningRow。
 */

afterEach(cleanup);

const LONG = "第一行想法\n第二行想法\n最新想到的这一点";

describe("思考行", () => {
  it("标题固定是 Think，摘要取第一行", () => {
    const view = render(<ThinkingBlock text={"先算一下\n再回答"} />);
    const button = view.getByRole("button", { name: /Think/ });

    expect(button.textContent).toContain("Think");
    expect(button.textContent).toContain("先算一下");
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("流式时摘要取**最后一行**，跟着思考往前走", () => {
    const view = render(<ThinkingBlock running text={LONG} />);
    const button = view.getByRole("button", { name: /Think/ });

    expect(button.textContent).toContain("最新想到的这一点");
    expect(button.textContent).not.toContain("第一行想法");
  });

  it("正文默认不挂载，点一下才展开全文", () => {
    const view = render(<ThinkingBlock text={LONG} />);
    expect(view.queryByText(/第二行想法/)).toBeNull();

    fireEvent.click(view.getByRole("button", { name: /Think/ }));

    expect(view.getByText(/第二行想法/)).toBeTruthy();
    expect(view.getByRole("button", { name: /Think/ }).getAttribute("aria-expanded")).toBe("true");
  });

  it("流式时给读屏一句“正在思考”", () => {
    const view = render(<ThinkingBlock running text={"想"} />);
    expect(view.getByText("正在思考")).toBeTruthy();
  });

  it("结束后不再播报，也不会自动展开", () => {
    const view = render(<ThinkingBlock text={"想完了"} />);
    expect(view.queryByText("正在思考")).toBeNull();
    // 折叠时正文不挂载：只有那一行摘要里出现这几个字
    expect(view.getAllByText(/想完了/)).toHaveLength(1);
    expect(view.queryByRole("button", { name: /Think/ })?.getAttribute("aria-expanded")).toBe("false");
  });
});
