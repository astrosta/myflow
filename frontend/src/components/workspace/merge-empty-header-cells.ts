/**
 * Backend HTML→GFM conversion expands colspan/rowspan into empty placeholder
 * cells so pipe tables stay rectangular (see knowledge/text.py). GFM cannot
 * express the merges, so the frontend restores them for display only.
 *
 * Dual-layer detection is intentionally conservative: only when the first body
 * row has a leading empty prefix (rowspan candidates) AND the remaining header
 * columns include empty colspan placeholders. Plain tables with sparse first
 * data rows are left untouched.
 */

export type HastNode = {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
  value?: string;
};

function cellText(node: HastNode): string {
  if (node.type === "text") return node.value ?? "";
  return (node.children ?? []).map(cellText).join("");
}

function isEmpty(node: HastNode): boolean {
  return cellText(node).trim() === "";
}

function isElement(node: HastNode, tagName: string): boolean {
  return node.type === "element" && node.tagName === tagName;
}

function elementChildren(node: HastNode, tagName: string): HastNode[] {
  return (node.children ?? []).filter((child) => isElement(child, tagName));
}

function rowCells(row: HastNode): HastNode[] {
  return (row.children ?? []).filter(
    (child) => isElement(child, "th") || isElement(child, "td"),
  );
}

function leadingEmptyCount(cells: HastNode[]): number {
  let count = 0;
  for (const cell of cells) {
    if (!isEmpty(cell)) break;
    count += 1;
  }
  return count;
}

/**
 * MinerU-style dual header after GFM flattening, e.g.
 *   | 类别 | 行员级别 | 考核分值 | 准入标准 |  |  |
 *   |  |  |  | 储蓄业务 | 个贷业务 | 卡业务 |
 */
function isDualLayerHeader(header: HastNode[], sub: HastNode[]): boolean {
  if (header.length === 0 || header.length !== sub.length) return false;
  const k = leadingEmptyCount(sub);
  if (k < 1 || k >= header.length) return false;
  for (let i = 0; i < k; i += 1) {
    if (isEmpty(header[i]!)) return false;
  }
  for (let i = k; i < sub.length; i += 1) {
    if (isEmpty(sub[i]!)) return false;
  }
  // Require colspan placeholders under the sub-header region so sparse data
  // rows like "|  |  | 北京 |" are not promoted.
  return header.slice(k).some((cell) => isEmpty(cell));
}

function mergeTrailingEmptyCells(row: HastNode, cellTag: "th" | "td"): void {
  const children = row.children ?? [];
  const next: HastNode[] = [];
  let index = 0;
  while (index < children.length) {
    const node = children[index]!;
    if (!isElement(node, cellTag) || isEmpty(node)) {
      next.push(node);
      index += 1;
      continue;
    }
    let span = 1;
    let look = index + 1;
    while (look < children.length) {
      const sibling = children[look]!;
      if (sibling.type === "text" && !(sibling.value ?? "").trim()) {
        look += 1;
        continue;
      }
      if (isElement(sibling, cellTag) && isEmpty(sibling)) {
        span += 1;
        look += 1;
        continue;
      }
      break;
    }
    if (span > 1) {
      node.properties = { ...(node.properties ?? {}), colSpan: span };
      next.push(node);
      index = look;
      continue;
    }
    next.push(node);
    index += 1;
  }
  row.children = next;
}

function toHeaderCell(cell: HastNode): HastNode {
  return {
    ...cell,
    tagName: "th",
    properties: { ...(cell.properties ?? {}) },
  };
}

function restoreDualLayerHeader(thead: HastNode, tbody: HastNode): boolean {
  const headerRows = elementChildren(thead, "tr");
  const bodyRows = elementChildren(tbody, "tr");
  if (headerRows.length !== 1 || bodyRows.length < 1) return false;

  const topRow = headerRows[0]!;
  const subRow = bodyRows[0]!;
  const header = rowCells(topRow);
  const sub = rowCells(subRow);
  if (!isDualLayerHeader(header, sub)) return false;

  const k = leadingEmptyCount(sub);
  for (let i = 0; i < k; i += 1) {
    const cell = header[i]!;
    cell.properties = { ...(cell.properties ?? {}), rowSpan: 2 };
  }

  mergeTrailingEmptyCells(topRow, "th");

  const subHeaderCells = sub.slice(k).map(toHeaderCell);
  const newSubRow: HastNode = {
    type: "element",
    tagName: "tr",
    properties: { ...(subRow.properties ?? {}) },
    children: subHeaderCells,
  };
  thead.children = [...(thead.children ?? []).filter((child) => child !== topRow), topRow, newSubRow];

  const remainingBody = (tbody.children ?? []).filter((child) => child !== subRow);
  tbody.children = remainingBody;
  return true;
}

function processTable(table: HastNode): void {
  const thead = elementChildren(table, "thead")[0];
  const tbody = elementChildren(table, "tbody")[0];
  if (thead && tbody && restoreDualLayerHeader(thead, tbody)) return;

  if (thead) {
    for (const row of elementChildren(thead, "tr")) {
      mergeTrailingEmptyCells(row, "th");
    }
  }
}

export function mergeEmptyHeaderCellsInTree(tree: HastNode): void {
  const stack: HastNode[] = [tree];
  while (stack.length > 0) {
    const node = stack.pop()!;
    if (isElement(node, "table")) {
      processTable(node);
      continue;
    }
    for (const child of node.children ?? []) stack.push(child);
  }
}

/** rehype plugin: restore colspan/rowspan flattened away by GFM tables. */
export function rehypeMergeEmptyHeaderCells() {
  return (tree: HastNode) => {
    mergeEmptyHeaderCellsInTree(tree);
  };
}
