/**
 * DOM 测试环境的最小 setup（M5 §8.1）。
 *
 * DOM 环境大约是 node 的 3 倍耗时，所以这里只放真正需要 document 的东西：
 * 取消动画（happy-dom 下 `matchMedia` 和 rAF 的差异会让断言不稳定）。
 */

if (typeof window !== "undefined") {
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
