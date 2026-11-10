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
