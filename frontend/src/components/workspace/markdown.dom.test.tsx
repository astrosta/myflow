import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Markdown } from "./markdown";

afterEach(cleanup);

describe("Markdown 表格", () => {
  it("把 GFM 表头里 colspan 占位空单元格合并显示", async () => {
    render(
      <Markdown
        text={
          "| 类别 | 行员级别 | 考核分值 | 准入标准 |  |  |\n" +
          "| --- | --- | --- | --- | --- | --- |\n" +
          "|  |  |  | 储蓄业务 | 个贷业务 | 卡业务 |"
        }
      />,
    );

    const table = await screen.findByRole("table");
    const thead = table.querySelector("thead");
    expect(thead).toBeTruthy();
    const headers = within(thead as HTMLElement).getAllByRole("columnheader");
    expect(headers.map((cell) => cell.textContent?.trim())).toEqual([
      "类别",
      "行员级别",
      "考核分值",
      "准入标准",
      "储蓄业务",
      "个贷业务",
      "卡业务",
    ]);
    expect(headers[0]?.getAttribute("rowspan")).toBe("2");
    expect(headers[3]?.getAttribute("colspan")).toBe("3");
    expect(table.querySelector("tbody")?.querySelectorAll("tr")).toHaveLength(0);
  });

  it("行跨展平后的数据行仍与列一一对应", async () => {
    render(
      <Markdown
        text={
          "| 类别 | 行员级别 | 考核分值 | 准入标准 |  |  |\n" +
          "| --- | --- | --- | --- | --- | --- |\n" +
          "|  |  |  | 储蓄业务 | 个贷业务 | 卡业务 |\n" +
          "| 客户经理助理 | 5 | 90 | 300万 |  | 500 张 |\n" +
          "| 客户经理助理 | 4 | 95 | 300万 |  | 500 张 |"
        }
      />,
    );

    const table = await screen.findByRole("table");
    const tbody = table.querySelector("tbody") as HTMLElement;
    const rows = within(tbody).getAllByRole("row");
    expect(rows).toHaveLength(2);
    expect(
      rows.map((row) =>
        within(row)
          .getAllByRole("cell")
          .map((cell) => cell.textContent?.trim()),
      ),
    ).toEqual([
      ["客户经理助理", "5", "90", "300万", "", "500 张"],
      ["客户经理助理", "4", "95", "300万", "", "500 张"],
    ]);
  });

  it("普通表格保持原样，不把首行数据误当成双层表头", async () => {
    render(
      <Markdown
        text={
          "| 姓名 | 年龄 | 城市 |\n" +
          "| --- | --- | --- |\n" +
          "|  |  | 北京 |\n" +
          "| 张三 | 30 | 上海 |"
        }
      />,
    );

    const table = await screen.findByRole("table");
    const thead = table.querySelector("thead");
    const headers = within(thead as HTMLElement).getAllByRole("columnheader");
    expect(headers.map((cell) => cell.textContent?.trim())).toEqual(["姓名", "年龄", "城市"]);
    expect(headers.every((cell) => cell.getAttribute("rowspan") == null)).toBe(true);
    expect(within(table.querySelector("tbody") as HTMLElement).getAllByRole("row")).toHaveLength(
      2,
    );
  });
});
