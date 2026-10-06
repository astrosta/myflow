"use client";

import { memo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

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

const REMARK_PLUGINS = [remarkGfm];

const COMPONENTS: Components = {
  a({ node, ...props }) {
    return <a {...props} rel="noreferrer noopener" target="_blank" />;
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
  /** 还在流式追加，末尾挂一个光标 */
  streaming?: boolean;
  className?: string;
}

export const Markdown = memo(function Markdown({ text, streaming = false, className }: MarkdownProps) {
  return (
    <div className={cn("md-body", className)}>
      <ReactMarkdown components={COMPONENTS} remarkPlugins={REMARK_PLUGINS}>
        {text}
      </ReactMarkdown>
      {streaming ? <span aria-hidden className="streaming-caret" /> : null}
    </div>
  );
});
