import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

/**
 * 每个渲染 ChatRoute 的 page 都必须自己加载聊天样式。
 *
 * Next 的样式边界是按路由段拆的；/workspace 和 /workspace/chats/[id] 是两个
 * page，新会话首轮只改地址栏（history.replaceState）不会切路由段，所以
 * /workspace 少引一份 markdown.css，就会出现"首轮字号/间距不对，刷新后正常"。
 */
const APP_ROOT = fileURLToPath(new URL("../..", import.meta.url));

function pageFiles(directory: string): string[] {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    const stat = statSync(path);
    if (stat.isDirectory()) return pageFiles(path);
    return name === "page.tsx" ? [path] : [];
  });
}

function importsCss(source: string, pattern: RegExp): boolean {
  return pattern.test(source);
}

const MARKDOWN_CSS = /import\s+["'][^"']*markdown\.css["']/;
const KATEX_CSS = /import\s+["']katex\/dist\/katex\.min\.css["']/;
const CHAT_CSS = /import\s+["'][^"']*chat\.css["']/;

describe("ChatRoute 页面的样式边界", () => {
  const chatPages = pageFiles(APP_ROOT).filter((path) =>
    readFileSync(path, "utf8").includes("ChatRoute"),
  );

  it("至少找到两个聊天入口", () => {
    expect(chatPages.length).toBeGreaterThanOrEqual(2);
  });

  for (const path of chatPages) {
    it(`${path} 加载 markdown / katex / 流式样式`, () => {
      const source = readFileSync(path, "utf8");
      expect(importsCss(source, MARKDOWN_CSS)).toBe(true);
      expect(importsCss(source, KATEX_CSS)).toBe(true);
      expect(importsCss(source, CHAT_CSS)).toBe(true);
    });
  }
});
