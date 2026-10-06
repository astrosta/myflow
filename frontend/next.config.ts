import type { NextConfig } from "next";

/**
 * 网关地址不在这里配置，浏览器侧一律走同源 `/api`（见 src/core/api/base.ts）。
 *
 * 生产：nginx 把 `/api/` 反代到网关（docker/nginx.conf），Next 根本看不到这些请求。
 * 开发：没有 nginx，所以必须由 Next 自己把 `/api/*` 转出去——否则浏览器打的是
 *       `http://127.0.0.1:3000/api/...`，Next 没有这个路由，直接 404，
 *       表现就是"点了发送没反应"。
 *
 * 走 rewrite 而不是让前端直连 `http://127.0.0.1:8001`，是为了**保持同源**：
 * 跨源就要后端配 CORS，而 M5 §7 的前提本来就是"浏览器侧只认一个同源入口"。
 */
const GATEWAY_INTERNAL_URL = process.env.GATEWAY_INTERNAL_URL ?? "http://127.0.0.1:8001";

const nextConfig: NextConfig = {
  /**
   * `standalone` 是 `docker/Dockerfile` 的前提：它把 Next 的 server 和它实际用到的
   * node_modules 子集打到 `.next/standalone`，runner 阶段就不用再带一份完整依赖树。
   * 本地 `next dev` / `next build` 的行为不变。
   */
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  /**
   * Next 16 的开发服务器**默认拦截跨源的 /_next/* 请求**，因为它认为自己的
   * origin 是 localhost。而 Playwright / 浏览器用的是 127.0.0.1 —— 两者字面不同，
   * 于是客户端 bundle 被拦，页面永远停在 SSR 那一帧：输入框是 disabled 的、
   * 一个 /api 请求都不会发出去（e2e 五条用例全卡在这里）。
   *
   * 只在开发期生效，生产构建完全不受影响。
   */
  allowedDevOrigins: ["127.0.0.1", "localhost"],
  experimental: {
    // 聊天页的 markdown/代码高亮只挂在 /workspace/chats/*，不要泄漏到落地页
    optimizePackageImports: ["shiki"],
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${GATEWAY_INTERNAL_URL}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
