"use client";

import { memo } from "react";
import rehypeKatex from "rehype-katex";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";

import { cn } from "@/lib/utils";

/**
 * Markdown 渲染（M5 §6.4）。
 *
 * **刻意不做逐词动画。** 一个很自然的写法是加个 rehype 插件把每个词包成 span，
 * 但流式追加会让整个块重新解析：旧的词重新挂载、动画整体重放一遍，
 * 表现为文字不停闪烁。这里只用 react-markdown 自身的增量重渲染
 * （追加一个 token 就重渲染这一块），配合 memo 让未变化的块不重算。
 *
 * 组件映射和 plugins 数组都提到模块级：它们在每次渲染时换新引用的话，
 * memo 会被整体绕过，流式期间每个 token 都要重建整棵 markdown 树。
 */

const REMARK_PLUGINS = [remarkGfm, remarkMath];
const REHYPE_PLUGINS = [rehypeKatex];

/**
 * remark-math 把同一行里的 `$$...$$` 当行内公式；检索文档里常见的
 * `$$ formula $$` 独占一行时，作者通常想要的是 display math。这里只
 * 改写代码围栏之外的独立行，其他位置保持原样。
 */
function normalizeDisplayMath(text: string): string {
  let fenced = false;
  return text
    .split("\n")
    .map((line) => {
      if (/^\s*(```|~~~)/.test(line)) {
        fenced = !fenced;
        return line;
      }
      if (fenced) return line;
      const match = /^[ \t]*\$\$[ \t]*(.+?)[ \t]*\$\$[ \t]*$/.exec(line);
      return match ? "$$\n" + match[1] + "\n$$" : line;
    })
    .join("\n");
}

const COMPONENTS: Components = {
  a({ node, ...props }) {
    const internal = typeof props.href === "string" && props.href.startsWith("/");
    return (
      <a
        {...props}
        rel={internal ? undefined : "noreferrer noopener"}
        target={internal ? undefined : "_blank"}
      />
    );
  },
  table({ node, ...props }) {
    // 宽表格自己滚，不要把整页撑出横向滚动条
    return (
      <div className="md-table-wrap">
        <table {...props} />
      </div>
    );
  },
};

export interface MarkdownProps {
  text: string;
  className?: string;
}

/**
 * 正文渲染。**不挂流式光标**：逐 token 追加时那个闪烁方块会一直贴在文字末尾，
 * 分辨率低的时候比正文还显眼；"还在写"这件事由步骤面板的"处理中…"和思考行的
 * 呼吸动画表达就够了。
 */
export const Markdown = memo(function Markdown({ text, className }: MarkdownProps) {
  return (
    <div className={cn("md-body", className)}>
      <ReactMarkdown
        components={COMPONENTS}
        rehypePlugins={REHYPE_PLUGINS}
        remarkPlugins={REMARK_PLUGINS}
      >
        {normalizeDisplayMath(text)}
      </ReactMarkdown>
    </div>
  );
});
