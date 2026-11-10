import { describe, expect, it } from "vitest";

import { mergeEmptyHeaderCellsInTree } from "./merge-empty-header-cells";

type HastNode = {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
  value?: string;
};

function text(value: string): HastNode {
  return { type: "text", value };
}

function cell(
  tagName: "th" | "td",
  value: string,
  properties: Record<string, unknown> = {},
): HastNode {
  return { type: "element", tagName, properties, children: [text(value)] };
}

function th(value: string, properties: Record<string, unknown> = {}): HastNode {
  return cell("th", value, properties);
}

function td(value: string): HastNode {
  return cell("td", value);
}

function row(tagName: "th" | "td", values: string[]): HastNode {
  return {
    type: "element",
    tagName: "tr",
    properties: {},
    children: values.map((value) => cell(tagName, value)),
  };
}

function tableTree(header: string[], bodyRows: string[][]): HastNode {
  return {
    type: "root",
    children: [
      {
        type: "element",
        tagName: "table",
        properties: {},
        children: [
          {
            type: "element",
            tagName: "thead",
            properties: {},
            children: [row("th", header)],
          },
          {
            type: "element",
            tagName: "tbody",
            properties: {},
            children: bodyRows.map((values) => row("td", values)),
          },
        ],
      },
    ],
  };
}

function tableWithHeader(cells: string[]): HastNode {
  return {
    type: "root",
    children: [
      {
        type: "element",
        tagName: "table",
        properties: {},
        children: [
          {
            type: "element",
            tagName: "thead",
            properties: {},
            children: [row("th", cells)],
          },
        ],
      },
    ],
  };
}

function headerCells(tree: HastNode): HastNode[] {
  const table = tree.children?.[0];
  const thead = table?.children?.find((child) => child.tagName === "thead");
  return (thead?.children ?? []).flatMap((tr) =>
    (tr.children ?? []).filter((child) => child.tagName === "th"),
  );
}

function bodyRows(tree: HastNode): HastNode[] {
  const table = tree.children?.[0];
  const tbody = table?.children?.find((child) => child.tagName === "tbody");
  return (tbody?.children ?? []).filter((child) => child.tagName === "tr");
}

function cellValues(nodes: HastNode[]): string[] {
  return nodes.map((node) => node.children?.[0]?.value ?? "");
}

describe("mergeEmptyHeaderCellsInTree", () => {
  it("把非空表头后紧跟的空 th 合并成 colspan", () => {
    const tree = tableWithHeader(["类别", "行员级别", "考核分值", "准入标准", "", ""]);

    mergeEmptyHeaderCellsInTree(tree);

    const headers = headerCells(tree);
    expect(cellValues(headers)).toEqual(["类别", "行员级别", "考核分值", "准入标准"]);
    expect(headers[3]?.properties?.colSpan).toBe(3);
  });

  it("不改动没有空占位的表头", () => {
    const tree = tableWithHeader(["参数", "类型", "说明"]);

    mergeEmptyHeaderCellsInTree(tree);

    const headers = headerCells(tree);
    expect(headers).toHaveLength(3);
    expect(headers.every((cellNode) => cellNode.properties?.colSpan == null)).toBe(true);
  });

  it("识别双层表头：前三列 rowspan，准入标准 colspan，子表头升入 thead", () => {
    const tree = tableTree(
      ["类别", "行员级别", "考核分值", "准入标准", "", ""],
      [
        ["", "", "", "储蓄业务", "个贷业务", "卡业务"],
        ["客户经理助理", "5", "90", "300万", "", "500张"],
      ],
    );

    mergeEmptyHeaderCellsInTree(tree);

    const table = tree.children?.[0];
    const thead = table?.children?.find((child) => child.tagName === "thead");
    const headerRows = (thead?.children ?? []).filter((child) => child.tagName === "tr");
    expect(headerRows).toHaveLength(2);

    const top = (headerRows[0]?.children ?? []).filter((child) => child.tagName === "th");
    expect(cellValues(top)).toEqual(["类别", "行员级别", "考核分值", "准入标准"]);
    expect(top[0]?.properties?.rowSpan).toBe(2);
    expect(top[1]?.properties?.rowSpan).toBe(2);
    expect(top[2]?.properties?.rowSpan).toBe(2);
    expect(top[3]?.properties?.colSpan).toBe(3);
    expect(top[3]?.properties?.rowSpan).toBeUndefined();

    const sub = (headerRows[1]?.children ?? []).filter((child) => child.tagName === "th");
    expect(cellValues(sub)).toEqual(["储蓄业务", "个贷业务", "卡业务"]);

    const rows = bodyRows(tree);
    expect(rows).toHaveLength(1);
    const data = (rows[0]?.children ?? []).filter((child) => child.tagName === "td");
    expect(cellValues(data)).toEqual(["客户经理助理", "5", "90", "300万", "", "500张"]);
  });

  it("普通表格首行数据有空单元格时不提升为双层表头", () => {
    const tree = tableTree(
      ["姓名", "年龄", "城市"],
      [
        ["", "", "北京"],
        ["张三", "30", "上海"],
      ],
    );

    mergeEmptyHeaderCellsInTree(tree);

    const table = tree.children?.[0];
    const thead = table?.children?.find((child) => child.tagName === "thead");
    expect((thead?.children ?? []).filter((child) => child.tagName === "tr")).toHaveLength(1);
    expect(bodyRows(tree)).toHaveLength(2);
    expect(headerCells(tree).every((cellNode) => cellNode.properties?.rowSpan == null)).toBe(
      true,
    );
  });

  it("普通数据行中间的空单元格不会被横向合并", () => {
    const tree = tableTree(
      ["储蓄业务", "个贷业务", "卡业务"],
      [["300万", "", "500张"]],
    );

    mergeEmptyHeaderCellsInTree(tree);

    const data = (bodyRows(tree)[0]?.children ?? []).filter((child) => child.tagName === "td");
    expect(cellValues(data)).toEqual(["300万", "", "500张"]);
    expect(data.every((cellNode) => cellNode.properties?.colSpan == null)).toBe(true);
  });
});
