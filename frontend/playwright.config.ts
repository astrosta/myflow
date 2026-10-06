import { defineConfig, devices } from "@playwright/test";

/**
 * M5 §8.2 的端到端配置。
 *
 * 两条硬约束：
 *
 * 1. **不依赖真实 Gateway。** chat.spec.ts 里所有 /api 请求都被网络拦截替换成
 *    手写的 SSE / JSON fixture。E2E 一旦连真后端，后端任何一次改动都会让它变红，
 *    然后所有人就开始忽略它（§8.2 最后一段）。这里也因此**不需要** myflow-gateway 起来。
 *
 * 2. **不改任何默认端口。** Next dev server 只在 E2E_PORT（默认 3000）上起，
 *    和 docker-compose 里发布的 2026 互不干扰；需要对着已经跑起来的部署测时，
 *    用 E2E_BASE_URL 指过去，这时 Playwright 不会再自己起服务。
 *
 * 跑法（浏览器已经装好之后）：
 *   pnpm e2e                       # 自带 next dev
 *   E2E_BASE_URL=http://127.0.0.1:2026 pnpm e2e   # 对着 compose 起来的栈跑
 */

const PORT = Number(process.env.E2E_PORT ?? "3000");
const BASE_URL = process.env.E2E_BASE_URL ?? "http://127.0.0.1:" + PORT;
/** 没有外部给的 baseURL 时才由 Playwright 负责把 app 起起来 */
const MANAGE_SERVER = !process.env.E2E_BASE_URL;

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: process.env.CI ? [["github"], ["html", { open: "never" }]] : [["list"]],

  use: {
    baseURL: BASE_URL,
    locale: "zh-CN",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },

  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],

  ...(MANAGE_SERVER
    ? {
        webServer: {
          command: "pnpm exec next dev --port " + PORT,
          url: BASE_URL + "/",
          reuseExistingServer: !process.env.CI,
          timeout: 120_000,
        },
      }
    : {}),
});
