"use client";

import { useEffect, useRef, useState, type RefObject } from "react";

/**
 * 模态对话框的焦点契约：进入时把焦点交给对话框，Tab 在框内循环，Escape 关闭，
 * 关闭后把焦点还给打开它的那个元素。
 *
 * 三件事分开做是有原因的：
 * - **初始焦点**：不设的话键盘用户的 Tab 会从文档开头重新数一遍，读屏器也不会自动
 *   播报对话框内容；默认给"第一个可聚焦元素"，也可以用 data-initial-focus 指定。
 * - **Tab 循环**：aria-modal="true" 只是承诺"外面不可交互"，浏览器不会替你实现；
 *   不拦 Tab 的话焦点会跑到背后的页面上（那里明明已经不可点了）。
 * - **焦点归还**：关闭后焦点掉回 <body>，键盘用户就得从头再来一遍。
 *
 * 已经落在框内的焦点不会被覆盖：异步渲染的表单字段可以先自己抢焦点，不会被对话框
 * 的"初始焦点"抢回去（子组件的 effect 先跑，父组件后跑）。
 */

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type=hidden])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function focusableIn(container: HTMLElement): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => element.tabIndex >= 0 && !element.hasAttribute("disabled"),
  );
}

export interface DialogFocusOptions {
  /** Escape 时调用；不传就只拦 Tab 与循环。 */
  onEscape?: () => void;
  /** 初始焦点选择器，默认 data-initial-focus。 */
  initialFocusAttribute?: string;
}

export function useDialogFocus(
  containerRef: RefObject<HTMLElement | null>,
  options: DialogFocusOptions = {},
): void {
  const { initialFocusAttribute = "data-initial-focus" } = options;
  const escapeRef = useRef(options.onEscape);
  /**
   * 触发器必须在**首次渲染时**就记住：effect 是子先父后跑的，表单里的字段会先把焦点
   * 抢走，等对话框的 effect 跑起来再读 activeElement，读到的已经是框内那个 input 了。
   */
  const [previous] = useState<HTMLElement | null>(() =>
    typeof document === "undefined" || !(document.activeElement instanceof HTMLElement)
      ? null
      : document.activeElement,
  );

  useEffect(() => {
    escapeRef.current = options.onEscape;
  }, [options.onEscape]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const active = document.activeElement;
    if (!(active instanceof HTMLElement) || !container.contains(active)) {
      const initial =
        container.querySelector<HTMLElement>("[" + initialFocusAttribute + "]") ??
        focusableIn(container)[0] ??
        container;
      initial.focus();
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        if (!escapeRef.current) return;
        event.preventDefault();
        escapeRef.current();
        return;
      }
      if (event.key !== "Tab") return;

      const items = focusableIn(container);
      if (items.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (!first || !last) return;
      const focused = document.activeElement;
      const inside = focused instanceof HTMLElement && container.contains(focused);
      if (event.shiftKey) {
        if (!inside || focused === first) {
          event.preventDefault();
          last.focus();
        }
      } else if (!inside || focused === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      // 触发元素可能已经不在了（比如"删除"后整行消失），focus 是安全的空操作。
      previous?.focus();
    };
  }, [containerRef, initialFocusAttribute, previous]);
}
