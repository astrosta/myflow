import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

const alias = { "@": fileURLToPath(new URL("./src", import.meta.url)) };

/**
 * 两个环境（M5 §8.1）：
 *   - node：纯逻辑（顺序合并、状态折叠、消息分组）。**应该是绝大多数**
 *   - dom：需要 document 的 hook / 组件。**尽量少**，DOM 环境大约是 node 的 3 倍耗时
 *
 * 反过来也有一条：一个行为只在真实 React 下存在的 hook（effect 顺序、卸载清理、
 * store 变化触发重渲染）必须写成 DOM 测试，而不是一个 mock 掉 React 的 node 测试。
 */
export default defineConfig({
  resolve: { alias },
  test: {
    projects: [
      {
        resolve: { alias },
        test: {
          name: "node",
          environment: "node",
          include: ["src/**/*.test.ts", "scripts/**/*.test.ts"],
          exclude: ["**/node_modules/**", "src/**/*.dom.test.ts", "src/**/*.dom.test.tsx"],
        },
      },
      {
        plugins: [react()],
        resolve: { alias },
        test: {
          name: "dom",
          environment: "happy-dom",
          include: ["src/**/*.dom.test.ts", "src/**/*.dom.test.tsx"],
          setupFiles: ["src/test/setup.dom.ts"],
        },
      },
    ],
  },
});
