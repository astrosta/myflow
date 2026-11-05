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
  /**
   * **必须关掉，否则开发期 SSE 不流式。**
   *
   * Next 的压缩层会给响应套 gzip；`text/event-stream` 没有 content-length，
   * 压缩流要攒够一块才 flush，于是 `/api/*` rewrite 把整条 SSE 压在手里，
   * 浏览器的表现是"一直转圈，答案最后一次性出现"。实测（300ms 一帧的慢速模型）：
   *   compress: true  → 14 帧全部在 run 结束那一刻到达（3.66s 一整坨）
   *   compress: false → 每 0.3s 到一帧，逐 token 渲染
   * 网关那边设的 `X-Accel-Buffering: no` 只管 nginx，管不到 Next 自己。
   * 生产压缩由 nginx 负责（`docker/nginx.conf` 的 gzip_types 故意不含
   * `text/event-stream`），所以这里关掉不会让生产多传字节。
   */
  compress: false,
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
    /**
     * 开发期 rewrite 默认 30 秒没有任何响应字节就掐断，并自己回 HTTP 500。
     * 技能安全扫描在 LLM 返回前整段请求都是静默的，大文件经常要 1–2 分钟，
     * 浏览器看到的 500 出在 Next，网关仍在跑，所以网关日志里什么都没有。
     * 和生产 nginx 的 /api proxy_read_timeout 对齐，都是 600 秒。
     */
    proxyTimeout: 600_000,
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
