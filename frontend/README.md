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

知识库页面在 `/workspace/knowledge`：创建时选择类型，详情页负责上传、状态轮询、
重新索引、删除和检索测试。后端未启用 `knowledge_base.enabled` 时页面会显示配置提示，
不会伪装成可上传。

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

## 登录标识

登录页那个输入框收的是 **`identifier`：邮箱或用户名**（M9 §2.4 的两个登录标识）。
后端按"有没有 `@"分派到不同的 unique 索引——用户名不允许 `@，所以永远不歧义。
输入框因此是 `type="text"` 而不是 `type="email"`。

- 注册 / 初始化要**同时**给邮箱和用户名（用户名 3-32 位，字母/数字/下划线/点/连字符）。
- 登录失败且后端说"这个账号还没注册"时，登录页会按输入内容给出"用这个邮箱注册"或
  "注册这个用户名"，并把刚敲的内容带进注册页预填。
- **改用户名**在设置面板的「账号」栏（`settings-dialog.tsx` → `updateUsername()`）：
  发 `PATCH /api/v1/auth/me`，body 只有 `{"username": "..."}`，拿回完整的
  `UserResponse` 直接更新本地状态（不必再拉一次 `/me`）。
  形状校验在 `core/auth/username.ts`，规则与后端 `normalize_username` **逐条对齐**——
  "前端说没问题、后端 422"是最难自查的一类不一致。
  这个请求前后端各有一条**协议**测试：`auth.contract.test.ts`（发出去的形状）与
  `test_auth_username_change.py`（收到的行为）。
- 顶栏用户菜单显示**用户名**（`title` 里带邮箱），因为用户名才是给人看的那个名字。

## 登录 / 注册的错误怎么显示

后端的错误体是 `{detail, code, trace_id}`，**`code` 才是给程序看的**（M9 §2.12）。
翻译集中在 `src/core/auth/error-messages.ts`，按 `code` 分支：

- 有稳定语义的 code → 固定的中文文案（`wrong_password`、`account_disabled`……）。
- **只有服务端才知道的细节**（邮箱是哪条规则没过、口令差几位 / 超几个字节）→ 直接展示
  `detail`。前端算不出这些数，硬编一句"格式不对"反而更难修。
- 认不出的 code → 显示服务端原话，不吞成一句笼统的失败。
- `login_rate_limited` → 用响应头 `Retry-After` 说出"还有 N 秒"。

登录页在 `email_not_registered` 时会多给一个「用这个邮箱注册」的入口（带 `?email=` 预填）。
`auth.local.specific_errors=false` 的部署拿不到这个 code，入口自然不出现——
UI 不会替用户断言"你没注册过"。

### 401 不等于"会话失效"

`src/core/api/unauthorized.ts` 里的 `isAuthEndpoint()` 把认证端点排除在
`notifyUnauthorized()` 的广播之外：登录 / 注册 / 初始化 / 改密上的 401、403 是
**"这次提交不对"**（密码错、邮箱没注册、当前密码错、注册关闭……），不是"会话没了"。

不排除的表现很具体：用户在登录页点一次登录，被 AuthGuard 当成会话过期弹走，
红框里写的是"登录状态已失效，请重新登录"——真正的原因被顶掉了。

`/v1/auth/me` **不在**排除名单里：它是探活，401 就是"会话没了"，该跳登录页。

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
