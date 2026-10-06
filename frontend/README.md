# M5 前端

MyFlow 的 Web 客户端。对接 M3 的 LangGraph Platform 兼容网关。

---

## 1. 启动（开发）

```bash
cd frontend
pnpm install
cp .env.example .env.local
pnpm dev                       # http://127.0.0.1:3000
```

首次 `pnpm dev` 大约 **10–40 秒**才 ready（Turbopack 要编译整棵树）；之后路由是按需编译的，
第一次访问某个路由会额外等几秒。实测：

```
✓ Ready in 21.8s
GET /                     200
GET /login                200
GET /workspace            200
GET /workspace/chats/t1   200   (首次编译约 10s)
```

### 工作区直接就是一个新会话

`/workspace` **不是**"点一下新建对话"的空状态，它本身就是一个还没落地的新会话：
进去就能打字。

**还没说话时输入框在页面中间**（上面配一句引导文案），发出第一条之后它落到页面底部，
消息列表在它上方展开。这个切换只看 `groups` 是否为空、且不在拉历史——
两个条件都要，否则刷新一个已有会话时会先闪一下引导态。

输入框在两种形态下**处在同一个子节点位置**，所以切换时 React 不会重建它，
草稿 / 已选附件 / 错误提示都不会丢。

发送按钮是**圆形箭头图标**（↑），和 DeepSeek、千问网页端一致：空输入时是灰底禁用，
一打字就变实心深色。流式期间它换成方形停止图标（■）。两个按钮的可访问名
分别是 `发送` / `停止`，来自 `aria-label`——e2e 和读屏都靠它，换图标时不能丢。

thread 在**第一次发送时**才由 SDK 创建（`threadId` 传 `null`），创建完回调
`onThreadCreated`，用 `history.replaceState` 把地址换成 `/workspace/chats/{id}`。
这里刻意**不用 `router.replace`**：那会让路由段重新挂载、`useStream` 重连一次，
刚发出去的消息所在的列表会闪一下。`history.replaceState` 不会重挂载，
而 Next 会同步 `usePathname`（`next/dist/client/components/app-router.js` 里明确
"Ensures usePathname and useSearchParams hold the newly provided url"），
所以侧栏高亮照样正确。

进页面就预建一个空 thread 是另一个选项，但那会往侧栏塞一堆没人用过的会话。
另外**第一条消息之前不能传附件**——上传需要一个已存在的 thread，
而"先建 thread 再提交"会让 SDK 再建一个，文件传到 A、对话跑在 B。

### 需要后端吗

**不需要也能起。** 前端是静态渲染 + 客户端取数，后端不在时页面照常打开，
对话框也能显示，只是发送会失败并在输入框上方显示错误。

### 发送走的是哪条路

浏览器侧永远只打**同源** `/api/*`（`src/core/api/base.ts`）。开发时没有 nginx，
所以由 `next.config.ts` 的 `rewrites()` 把 `/api/:path*` 转到 `GATEWAY_INTERNAL_URL`
（默认 `http://127.0.0.1:8001`）；生产时由 nginx 反代（`docker/nginx.conf`）。
这样**开发和生产是同一条同源路径**，不需要后端配 CORS。

点一次"发送"实际会打两个请求：

```
POST /api/threads                          ← 新会话时才发（SDK 建 thread）
POST /api/threads/{id}/runs/stream         ← SSE，消息从这里流回来
```

### 后端当前的阻塞点（截至本文，未在前端侧绕过）

前端链路已通（实测经 3000 的 `/api` 转发和直连 8001 返回完全一致），
但下面两条会让发送最终失败：

| 端点 | 现象 | 原因 |
|---|---|---|
| `POST /api/threads` | **404** | `backend/app/gateway/routers/threads.py` 里只有 docstring，thread CRUD 还没写；`app.py` 也只挂载了 `thread_runs` |
| `POST /api/threads/{id}/runs/stream` | **422** | `backend/app/gateway/run_models.py` 里 `checkpoint_id: str | None = Field(defaut=None, ...)` —— `defaut` 是 `default` 的拼写错误，pydantic 把它当成本字段的额外 schema 注解，于是 `checkpoint_id` 变成**必填**。同文件其它字段都写的 `default=` |

这两条都在后端侧，前端没有绕（也不该绕）。

### 端口

固定 `3000`（写死在 `package.json` 的 `dev`/`start` 里）。要换端口：

```bash
pnpm dev --port 3100
```

## 2. 启动（生产形态）

```bash
pnpm build                     # 约 40s
pnpm start:prod                # http://127.0.0.1:3000
```

### 为什么不是 `pnpm start`

`next.config.ts` 设了 `output: "standalone"`（Docker 镜像的前提）。这个模式下 Next 会明确警告：

```
⚠ "next start" does not work with "output: standalone" configuration.
  Use "node .next/standalone/server.js" instead.
```

而且 **standalone 产物不含 `.next/static` 和 `public/`**——不拷过去的话页面能返回 200，
但所有 JS/CSS 都是 404，白屏加一控制台 404。

`pnpm start:prod` 就是 `scripts/start-standalone.mjs`：先拷静态资源再起 `.next/standalone/server.js`，
**和 `docker/Dockerfile` 走完全同一条路径**。`pnpm start` 保留给"我就想快速看一眼"，能跑，但有上面那个警告。

实测 `pnpm start:prod`：

```
/                    -> 200
/login               -> 200
/workspace/chats/t1  -> 200
静态资源              -> 200
```

### ⚠ 别在 `pnpm build` 之后跑 `pnpm dev`

两者共用 `.next` 目录，`dev` 会用开发产物把它覆盖掉。之后再 `start` / `start:prod`
就会**满屏静态资源 404、页面白屏**，而且报错完全不指向真正的原因。切回去之前先清干净：

```bash
rm -rf .next && pnpm build
```

---

## 3. 环境变量

从 `.env.example` 复制一份 `.env.local`（已被 gitignore）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | `/api` | 浏览器侧前缀。**保持同源**，由 nginx 反代到网关；这样 CORS 和 SSE 的缓冲开关都不用前端感知 |
| `GATEWAY_INTERNAL_URL` | `http://127.0.0.1:8001` | 服务端直连网关用，不下发到浏览器 |
| `NEXT_PUBLIC_MAX_UPLOAD_BYTES` | `26214400` | 上传上限（25 MiB） |
| `NEXT_PUBLIC_ARTIFACT_PREVIEW_BYTES` | `1048576` | artifact 预览只取前 1 MiB（设计文档 §6.5） |

`NEXT_PUBLIC_*` 会在**构建时**内联进产物，改了要重新 build。

---
## 4. 其它命令

```bash
pnpm test          # node + dom 两个环境
pnpm test:node     # 只跑纯逻辑，快得多
pnpm test:watch
pnpm typecheck     # tsc --noEmit
pnpm lint          # eslint，应输出 0 problems
pnpm e2e           # playwright，需要先装浏览器
```

---

## 5. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `pnpm install` 报 `ERR_PNPM_IGNORED_BUILDS` | `pnpm-workspace.yaml` 的 `allowBuilds` 没填 | 见 §6.1 |
| 页面白屏、静态资源全 404 | `build` 之后跑过 `dev`，`.next` 被覆盖 | `rm -rf .next && pnpm build` |
| `pnpm start` 打 standalone 警告 | `output: "standalone"` | 用 `pnpm start:prod` |
| `EADDRINUSE :3000` | 端口被占 | 见下方「怎么杀干净」 |
| 侧栏 / 消息列表报错 | 网关没起，或网关还没有路由 | 预期行为，见 §1「需要后端吗」 |

### Windows / Git Bash 下怎么把服务杀干净

`pkill -f "next dev"` 在这套环境里**匹配不到 Windows 进程**，`kill $!` 也杀不掉 `pnpm`
派生的子进程。按端口找 PID 再 taskkill：

```bash
netstat -ano | grep ":3000 " | grep LISTENING        # 最后一列是 PID
taskkill //F //PID <PID>                             # Git Bash 里要写双斜杠
```

---

## 6. 已知问题

### 6.1 `pnpm-workspace.yaml` 里的 `allowBuilds` 曾经是个没填的占位值

pnpm 11 起**不再读 `package.json` 里的 `pnpm` 字段**（设置搬到了 `pnpm-workspace.yaml`，
`onlyBuiltDependencies` 也改名成了 `allowBuilds`）。之前那里留着一行：

```yaml
allowBuilds:
  unrs-resolver: set this to true or false     # ← 占位值，从来没填过
```

后果是 `pnpm install` 每次都以 `ERR_PNPM_IGNORED_BUILDS` 失败，**`pnpm dev` 直接起不来**
（pnpm 的依赖检查会隐式跑一次 install）。已改成 `unrs-resolver: true`。

这个坑同时影响 `docker/Dockerfile`——它在 alpine 上也要跑 `pnpm install`。

### 6.2 后端契约未定的三处假设

见文末「已知假设」一节。

---
## 7. 这个包的重心在 `core/`，不在 `components/`

设计文档（`docs/plans/2026-09-16-m5-frontend-design.md` §1）里写着 M5 的难点不在 UI。
真正的风险是**把"持久化的历史"和"实时的流"合并成一条正确排序的对话**——
这两个来源的顺序、完整性、身份语义完全不同，而且 M6 的上下文压缩会把它推向极端。

所以划分是这样的：

```
src/
├── app/                     路由。根布局是静态的，聊天页的重样式挂在聊天路由上
├── components/
│   ├── ui/                  基础件，从简手写，lint 忽略
│   └── workspace/           纯展示组件，只消费 ChatController，不认识 SDK
└── core/                    ← 重心。纯函数 + 接线，可以用 node 环境单测
    ├── api/                  SDK 单例、流模式净化、gap 恢复、分页 feed、上传
    ├── messages/             顺序合并、身份归一、消息分组、历史窗口
    └── threads/              updates 折叠、身份基线、useChat 装配
```

**`components/` 里不许出现 `useStream`、`fetch` 或排序逻辑。**
组件层唯一的状态入口是 `core/threads/chat-controller.ts` 的 `ChatController`。

## 8. 四条硬规则，以及它们落在哪

| 规则 | 位置 | 回归测试 |
|---|---|---|
| **位置权威：服务端 `seq` > 身份锚点编织 > 到达顺序** | `core/messages/order.ts` | `message-order.test.ts`（28 条） |
| **主聊天流强制增量模式，不带 `values`** | `core/api/stream-mode.ts` + `client.ts` 的 `onRequest` | `stream-mode.test.ts`、`stream-mode.contract.test.ts` |
| **`updates` 折叠必须和后端 reducer 语义一致** | `core/threads/stream-state.ts` | `stream-state.test.ts` |
| **gap 不能表现为一次正常的流结束** | `core/api/gap.ts` + `fetch-with-gap.ts` | `gap.test.ts`、`replay-gap.dom.test.tsx` |

### §4.3 那两次互相打架的修复

文档里说这是 M5 最值得讲的一段，因为它是一个**先后两次修复互相推翻**的真实过程：

1. 一条带 `seq` 的消息被追加到了列表尾部 → 尾部**可证明是错的**（有 `seq` 说明它在 feed 里有确定位置）→ 禁止追加。
2. 一个长会话里，用户自己的提问**凭空消失了** → 第一页历史已经够不到那条消息，而"禁止追加"的规则把它整个丢弃了。

和解的原则：**被折叠的未加载间隙可以靠翻页恢复；被丢弃的消息不能。**

所以既不能追加到尾部，也不能丢弃：一条 `seq` 低于当前加载窗口下界的实时消息，
放到屏幕上所有内容的**最前面**。而且这个判断**必须发生在锚点遍历之前**——
压缩后的 checkpoint 可能和加载页一条身份都不共享，锚点遍历根本不会执行。

`message-order.test.ts` 里的「压缩后零共享身份」那一条就是这条和解的守卫。
**没有它，下次重构一定会退回其中一种错误。**

## 9. 测试

| 环境 | 用于 | 说明 |
|---|---|---|
| **node**（`*.test.ts`） | 纯逻辑：顺序合并、状态折叠、消息分组、帧解析 | 绝大多数。DOM 环境大约慢 3 倍 |
| **dom**（`*.dom.test.ts(x)`，happy-dom） | 只在真实 React / Response / ReadableStream 下存在的行为 | 尽量少 |

`stream-mode.contract.test.ts` 会去读后端的
`backend/packages/harness/agentkit/runtime/stream_modes.py`，断言两侧的模式白名单
逐字一致。这种漂移在 code review 里看不出来（两份列表在两个语言、两个目录里），
所以让 CI 来对。

E2E（`e2e/chat.spec.ts`）**用网络拦截 mock 掉所有后端**，不依赖真实 Gateway——
那样的 E2E 会因为后端的任何改动而红，然后大家就开始忽略它。

## 10. 部署

`docker/nginx.conf` 里有两处**不能改**：

- `gzip_types` **故意不含** `text/event-stream`。被压缩或被缓冲的 SSE，
  体感就是"模型不流式了"——事件会被攒着批量下发。
- SSE location 上的 `proxy_buffering off` / `X-Accel-Buffering no` / 600s 超时。

`scripts/check-compose-ports.mjs` 扫描所有 compose 文件，断言每条 published port
都有显式的绑定地址。`"${PORT:-2026}:2026"` 和 `"127.0.0.1:${PORT:-2026}:2026"`
只差几个字符，但前者等于绑 `0.0.0.0`，肉眼 review 极容易漏。

## 11. 已知假设（后端契约还没定死的部分）

M3/M4/M6 的文档定义了协议语义，但有几处 HTTP 细节还没落地。前端按下面的假设实现，
每处都收在一个常量或函数里，契约定了只改一处：

| 假设 | 位置 | 依据 |
|---|---|---|
| `seq` 落在消息的哪个字段 | `core/api/adapt.ts` 的 `readSeq` | §4.2 说"后端在消息进入 feed 时盖一个线程全局的递增 seq"，但没定字段名。这里把 `seq` / `additional_kwargs.seq` / `response_metadata.seq` 都认。**认不出来的后果是排序退回锚点编织——能用，但不如 seq 优先确定。** |
| 消息 feed 的 HTTP 信封 | `core/api/messages-feed.ts` 的 `parseMessagePage` | M4 只定了 `list_messages` 的语义（升序、只返回 `category="message"`、双向游标）。这里对数组和 `{messages, has_more, next_before_seq}` 都认，**认不出来就抛错**——静默返回空数组会让"历史没了"表现为"这个会话是空的"。 |
| 文件上传端点 | `core/api/uploads.ts` 的 `UPLOAD_PATH` | M6 的 `UploadsMiddleware` 消费 `uploaded_files` 通道，但上传的 HTTP 端点在任何设计文档里都没出现过。默认 `POST /threads/{id}/uploads`（multipart）。 |

## 12. 刻意不做的事

Artifact 在线编辑、多面板/可拖拽布局、多语言、路由资源预算检查、静态演示模式、
结构化人机交互表单、分支与编辑重跑。理由见设计文档 §10。
