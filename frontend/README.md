# 前端服务

Next.js 工作区。浏览器只请求同源 `/api/*`；开发时 `next.config.ts` 把 `/api/:path*` rewrite 到 `GATEWAY_INTERNAL_URL`（默认 `http://127.0.0.1:8001`）。

## 开发

```bash
cd frontend
pnpm install
cp .env.example .env.local   # 首次
pnpm dev                     # http://127.0.0.1:3000
```

首次 ready 可能要十几秒（Turbopack）。打开 `/workspace` 就是一个尚未落库的新会话，第一条消息发出时才创建 thread，并用 `history.replaceState` 改成 `/workspace/chats/{id}`（不用 `router.replace`，避免 `useStream` 重连闪一下）。

**页面不依赖后端也能打开**；没起网关时发送会失败，错误显示在输入框上方。

固定端口 `3000`。换端口：`pnpm dev --port 3100`。

## 生产形态（standalone）

`next.config.ts` 使用 `output: "standalone"`。不要用普通的 `pnpm start` 当正式入口。

```bash
pnpm build
pnpm start:prod              # 拷静态资源后起 .next/standalone/server.js
```

`build` 之后不要再跑 `pnpm dev`：两者共用 `.next`，dev 会覆盖产物，随后 `start:prod` 静态资源全 404。要切回生产构建先删 `.next` 再 `pnpm build`。

Docker（对外一个端口，默认 `127.0.0.1:2026`）：

```bash
cd frontend/docker
docker compose up -d --build
```

compose 里网关只 `expose: 8001`，不要给它加 `ports`。发布端口必须写绑定地址（默认 loopback）。

## 环境变量

从 `.env.example` 复制为 `.env.local`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | `/api` | 浏览器前缀，保持同源 |
| `GATEWAY_INTERNAL_URL` | `http://127.0.0.1:8001` | 服务端 / rewrite 直连网关，不下发浏览器 |
| `NEXT_PUBLIC_MAX_UPLOAD_BYTES` | `26214400` | 上传上限 25 MiB |
| `NEXT_PUBLIC_ARTIFACT_PREVIEW_BYTES` | `1048576` | 预览只取前 1 MiB |

`NEXT_PUBLIC_*` 构建时内联，改了要重新 build。

## 发送时的请求

新会话会先建 thread，再开 SSE：

```text
POST /api/threads
POST /api/threads/{id}/runs/stream
```

主聊天流强制增量模式：`messages-tuple` + `updates` + `custom`，不带全量 `values`。

## 其它命令

```bash
pnpm test          # node + dom
pnpm test:node
pnpm typecheck
pnpm lint
pnpm e2e           # Playwright；e2e 拦截网络，不依赖真实网关
```

逻辑在 `src/core/`（排序、流模式、gap）。`src/components/workspace/` 只消费 `ChatController`，不要在组件里直接 `useStream` / `fetch`。

## 排障

| 现象 | 处理 |
|---|---|
| 点发送没请求 | 确认 `GATEWAY_INTERNAL_URL` 指向已启动的 8001；开发 rewrite 是否生效 |
| 白屏、静态资源 404 | `build` 后跑过 `dev`：删 `.next` 再 `pnpm build` |
| `EADDRINUSE :3000` | 换端口，或按 PID `taskkill` 掉占用进程 |
| SSE 不流式 | 生产 nginx 不能 gzip `text/event-stream`，且 `proxy_buffering off` |
