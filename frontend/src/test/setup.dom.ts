/**
 * DOM 测试环境的最小 setup（M5 §8.1）。
 *
 * DOM 环境大约是 node 的 3 倍耗时，所以这里只放真正需要 document 的东西：
 * 取消动画（happy-dom 下 `matchMedia` 和 rAF 的差异会让断言不稳定）。
 */

if (typeof window !== "undefined") {
  // happy-dom 没有实现 compatMode，而 KaTeX 在非 standards 模式下会直接
  // 拒绝渲染。浏览器里的文档一直是 CSS1Compat；测试环境补上同一个事实。
  if (document.compatMode !== "CSS1Compat") {
    Object.defineProperty(document, "compatMode", {
      configurable: true,
      value: "CSS1Compat",
    });
  }

  if (!window.matchMedia) {
    window.matchMedia = ((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
  }

  // 组件测试里不要真的滚动
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}
