import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { expect, test as base, type Page } from "@playwright/test";

/**
 * M5 §8.2 / §9 的端到端验收（浏览器行为）。
 *
 * 一、为什么不连真后端
 *   §8.2 最后一段：E2E 一旦依赖真实 Gateway，后端任何一次改动都会让它变红，
 *   然后所有人就开始忽略它。所以这里拦掉所有 /api 请求：
 *     - 非 SSE（JSON）→ page.route 里用 route.fulfill 返回手写 fixture
 *     - SSE → 转发到本文件里起的本地 mock gateway
 *
 * 二、为什么 SSE 不能只用 route.fulfill
 *   route.fulfill 是**一次性整包**下发：没有"先给一半、过一会再给另一半"的能力。
 *   而验收要的恰恰是"文字逐步出现"和"刷新后继续流式"——用 fulfill 的话所有帧
 *   在同一毫秒到达，React 一次批量更新就画完了，断言"第一段先出现"会变成随机
 *   通过/失败，"中途截断"（socket 直接断、不给 end）更是没法模拟。
 *   所以：page.route 仍然拦住所有 /api 请求，JSON 仍然用 route.fulfill，
 *   只有 SSE 那几个端点用 route.continue 转发到本地 mock gateway，
 *   由它按帧、按节奏、可暂停地写响应——这才能真实复现"流"这个行为。
 *   帧本身还是手写的，就在这个文件里：sseFrame / messagesFrame / gapFrame。
 *
 * 三、页面契约（这些可访问名/文案就是测试与 UI 之间的接口，改文案前先来这里改）
 *   路由：/workspace/chats/{thread_id}
 *   textbox  "消息输入框"          Composer 的输入框
 *   button   "发送"                非流式时的提交按钮（↑ 图标，名字来自 aria-label）
 *   button   "停止"                流式时替换掉提交按钮（§5.1）
 *   region   "处理过程"            StepsPanel：流式期间是"处理中…"，结束后是"已完成"
 *   article  "用户消息" / "助手回复"  HumanBubble / AnswerBubble
 *   region   "工具调用 <name>"      ToolCallRow（折叠时一行：工具名 + 参数摘要）
 *   button   "工具 <name>，<状态>"  ToolCallRow 那一行本身，点开才挂载参数和结果
 *   text     "正在生成…" / "就绪"   StreamStatus
 *   text     "连接中断，正在恢复…"  RecoveryBanner（§5.2，恢复期间才有）
 *
 * 四、没覆盖的（手工验收，§9 已经点明）
 *   - DevTools 里确认 SSE 响应没有 Content-Encoding: gzip
 *   - 触发一次真实的上下文压缩（需要真后端 + 真模型）
 *   - 写几百 KB 的大文件时分块而不是每 token 一帧
 */

const THREAD_ID = "e2e-thread-1";
const RUN_ID = "e2e-run-1";

/** UI 契约（见文件头第三节） */
const UI = {
  composerInput: "composer-input",
  composerSend: "composer-send",
  composerStop: "composer-stop",
  messageList: "message-list",
  message: "message",
  messageText: "message-text",
  runStatus: "run-status",
  recoveryNotice: "recovery-notice",
  toolRow: "tool-row",
  toolToggle: "tool-toggle",
  toolArgs: "tool-args",
  toolResult: "tool-result",
} as const;

/* -------------------------------------------------------------------------- */
/* SSE 编帧（M3 §8.1：字段顺序 event → data → id → 空行）                       */
/* -------------------------------------------------------------------------- */

function sseFrame(event: string, data: unknown, id?: number): string {
  const payload = typeof data === "string" ? data : JSON.stringify(data);
  const lines = ["event: " + event, "data: " + payload];
  if (id !== undefined) lines.push("id: " + String(id));
  return lines.join("\n") + "\n\n";
}

/** event: metadata：SDK 靠它确认 run 起来了（M3 §8.1） */
function metadataFrame(runId: string, threadId: string): string {
  return sseFrame("metadata", { run_id: runId, thread_id: threadId }, 0);
}

/**
 * event: messages 在 messages-tuple 模式下载荷是 [message, metadata]（§3.1 的主流模式），
 * seq 由服务端盖（§4.2）。
 */
function messagesFrame(message: unknown, seq: number): string {
  return sseFrame(
    "messages",
    [message, { seq: seq, run_id: RUN_ID, langgraph_node: "model", tags: [] }],
    seq,
  );
}

/** event: gap：M3 §7.3 的 replay gap，三个 id 都要有；gap 帧本身没有 id */
function gapFrame(latestEventId: string): string {
  return sseFrame("gap", {
    code: "stream_replay_gap",
    run_id: RUN_ID,
    requested_event_id: latestEventId,
    earliest_available_event_id: latestEventId,
    latest_available_event_id: latestEventId,
    recovery: "reload_durable_state",
  });
}

function humanMessage(text: string, seq: number): unknown {
  return { type: "human", id: "human-" + String(seq), content: text };
}

/**
 * LangChain 形状的 fixture -> 账本行（MessageResponse）。
 *
 * 真实网关的 content 就是完整序列化后的消息对象（带 id），
 * 前端正是靠这个 id 把"账本那一条"和"流里那一条"认成同一条（M5 §4.2）。
 */
function toLedgerRow(message: unknown, threadId: string): unknown {
  const m = (message ?? {}) as Record<string, unknown>;
  const seq = typeof m.seq === "number" ? m.seq : 1;
  const type = typeof m.type === "string" ? m.type : "ai";
  const eventType =
    type === "human" ? "llm.human.input" : type === "tool" ? "llm.tool.result" : "llm.ai.response";
  return {
    thread_id: threadId,
    run_id: typeof m.run_id === "string" ? m.run_id : RUN_ID,
    seq,
    event_type: eventType,
    content: {
      content: m.content ?? "",
      type,
      id: typeof m.id === "string" ? m.id : "m-" + String(seq),
      additional_kwargs: {},
      response_metadata: {},
    },
    metadata: {},
    created_at: "2026-09-16T00:00:00Z",
  };
}

function aiChunk(text: string, id: string): unknown {
  // 必须是 "ai"：SDK v1.12 的 dist/ui/messages.js 只在
  // `message.type === "ai" || "assistant"` 时才组装消息，LangChain 内部的
  // 类名 "AIMessageChunk" 会被整条丢掉（表现是流开着但一个字都不出现）。
  // 同一个文件里的 aiToolCallMessage 用的就是 "ai"。
  return { type: "ai", id: id, content: text };
}

function aiToolCallMessage(id: string, text: string, callId: string, name: string, args: unknown): unknown {
  return {
    type: "ai",
    id: id,
    content: text,
    tool_calls: [{ id: callId, name: name, args: args }],
  };
}

function toolResultMessage(id: string, callId: string, name: string, content: string): unknown {
  return { type: "tool", id: id, tool_call_id: callId, name: name, content: content, status: "success" };
}
/* -------------------------------------------------------------------------- */
/* mock gateway                                                               */
/* -------------------------------------------------------------------------- */

/** 一帧：可以带一个 gate（测试没放行前不写出去）与固定延迟 */
interface SseStep {
  text: string;
  gate?: string;
  delayMs?: number;
}

type Ending =
  /** 正常发 event: end 然后关闭 */
  | "end"
  /** 不发 end、不断开：run 还在跑（用来测刷新续流、停止） */
  | "hold"
  /** 直接掐断 socket，不给任何结束信号（模拟网关进程被杀） */
  | "truncate"
  /** 先发一帧 event: gap 再掐断（M3 §7.3：缓冲区滚过去了） */
  | "gap";

interface StreamPlan {
  frames: SseStep[];
  ending?: Ending;
  /** 断线/刷新之后重新订阅（/join 或 /stream）时下发的帧 */
  resumeFrames?: SseStep[];
  resumeEnding?: Ending;
}

interface ThreadFixture {
  messages: unknown[];
  runningRunId: string | null;
  cancelled: boolean;
  /** threads_meta.display_name 的投影；首轮结束后由 worker 写入 */
  displayName: string | null;
}

const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
  "Access-Control-Allow-Headers": "*",
  "Access-Control-Expose-Headers": "Content-Location,Content-Type",
};

/**
 * 认证探活的固定返回（M9 §2.5）。
 *
 * SessionGate / AuthGuard / UserMenu **各自持有一份 useSession()**，进工作区时
 * 会各发一次 GET /v1/auth/me。这几条不 mock 的话，用例末尾那句
 * expect(backend.unhandled).toEqual([]) 会红，而且红得很难看懂 ——
 * 报错是一串 auth 路径，和被测的聊天行为毫无关系。
 */
const AUTH_USER = {
  id: "u-e2e-1",
  email: "e2e@local.test",
  username: "e2e",
  system_role: "user",
};

class MockGateway {
  readonly threadId = THREAD_ID;
  readonly runId = RUN_ID;
  readonly log: { method: string; path: string }[] = [];
  readonly unhandled: string[] = [];

  private server: Server | null = null;
  private origin = "";
  private plan: StreamPlan = { frames: [], ending: "end" };
  private fixtures = new Map<string, ThreadFixture>();
  private gateOpen = new Set<string>();
  private gateWaiters = new Map<string, (() => void)[]>();
  private held: ServerResponse[] = [];

  createCount = 0;
  joinCount = 0;
  cancelCount = 0;
  stateReloadCount = 0;

  async start(): Promise<void> {
    this.server = createServer((req, res) => {
      void this.handle(req, res);
    });
    await new Promise<void>((resolve) => {
      this.server!.listen(0, "127.0.0.1", resolve);
    });
    const address = this.server.address() as AddressInfo;
    this.origin = "http://127.0.0.1:" + String(address.port);
  }

  async close(): Promise<void> {
    for (const res of this.held.splice(0)) res.destroy();
    const server = this.server;
    this.server = null;
    if (!server) return;
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }

  setPlan(plan: StreamPlan): void {
    this.plan = plan;
  }

  /** 让 GET /threads/{tid}/runs 返回一个 running 的 run */
  markRunRunning(threadId = THREAD_ID): void {
    this.fixture(threadId).runningRunId = RUN_ID;
  }

  /** 持久化历史（刷新后 join 时 UI 会从这里读回来） */
  seedMessages(threadId: string, messages: unknown[]): void {
    this.fixture(threadId).messages = messages;
  }

  /** 模拟 worker 收尾时把 checkpoint title 投影成列表里的 display_name */
  setThreadTitle(title: string | null, threadId = THREAD_ID): void {
    this.fixture(threadId).displayName = title;
  }

  /** 测试放行一个 gate（mock 停在那里等） */
  release(gate: string): void {
    this.gateOpen.add(gate);
    const waiters = this.gateWaiters.get(gate) ?? [];
    this.gateWaiters.delete(gate);
    for (const resolve of waiters) resolve();
  }

  /** route.continue 需要完整 URL */
  originFor(pathname: string, search: string): string {
    return this.origin + pathname + search;
  }

  /* ------------------------------ 内部实现 ------------------------------ */

  private fixture(threadId: string): ThreadFixture {
    let fixture = this.fixtures.get(threadId);
    if (!fixture) {
      fixture = { messages: [], runningRunId: null, cancelled: false, displayName: null };
      this.fixtures.set(threadId, fixture);
    }
    return fixture;
  }

  private waitForGate(gate: string): Promise<void> {
    if (this.gateOpen.has(gate)) return Promise.resolve();
    return new Promise<void>((resolve) => {
      const waiters = this.gateWaiters.get(gate) ?? [];
      waiters.push(resolve);
      this.gateWaiters.set(gate, waiters);
    });
  }

  private async handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
    const method = req.method ?? "GET";
    const url = new URL(req.url ?? "/", this.origin);
    const path = url.pathname;
    this.log.push({ method: method, path: path });

    if (method === "OPTIONS") {
      res.writeHead(204, CORS_HEADERS);
      res.end();
      return;
    }

    const threadId = /\/threads\/([^/]+)/.exec(path)?.[1] ?? THREAD_ID;

    if (this.isStreamPath(path)) {
      await this.stream(req, res, threadId);
      return;
    }

    // 非 SSE 的请求本来应该被 page.route 用 fulfill 接走（见 interceptApi）。
    // 这里兜底 501：漏拦截时是明确的报错，而不是"页面莫名其妙空白"。
    this.unhandled.push(method + " " + path);
    res.writeHead(501, { "Content-Type": "application/json", ...CORS_HEADERS });
    res.end(JSON.stringify({ error: "mock gateway only serves SSE: " + method + " " + path }));
  }

  isStreamPath(path: string): boolean {
    if (/^\/api\/threads\/[^/]+\/runs\/stream$/.test(path)) return true;
    if (/^\/api\/threads\/[^/]+\/runs\/[^/]+\/stream$/.test(path)) return true;
    if (/^\/api\/threads\/[^/]+\/runs\/[^/]+\/join$/.test(path)) return true;
    if (/^\/api\/threads\/[^/]+\/runs\/wait$/.test(path)) return true;
    return false;
  }

  private async stream(req: IncomingMessage, res: ServerResponse, threadId: string): Promise<void> {
    const isCreate = (req.method ?? "GET") === "POST";
    if (isCreate) this.createCount += 1;
    else this.joinCount += 1;

    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
      // M3 §8.1 的响应头；nginx 侧 §7.2 也要求这个
      "X-Accel-Buffering": "no",
      "Content-Location": "/api/threads/" + threadId + "/runs/" + RUN_ID,
      ...CORS_HEADERS,
    });
    res.socket?.setNoDelay(true);
    res.flushHeaders();

    const steps = isCreate ? this.plan.frames : (this.plan.resumeFrames ?? []);
    const ending: Ending = isCreate
      ? (this.plan.ending ?? "end")
      : (this.plan.resumeEnding ?? "end");

    for (const step of steps) {
      if (step.gate) await this.waitForGate(step.gate);
      if (step.delayMs) await new Promise((resolve) => setTimeout(resolve, step.delayMs));
      if (res.destroyed || res.writableEnded) return;
      res.write(step.text);
    }

    if (ending === "hold") {
      // run 还在跑：连接留着，等测试自己决定什么时候结束
      this.held.push(res);
      res.on("close", () => {
        this.held = this.held.filter((item) => item !== res);
      });
      return;
    }
    if (ending === "gap") {
      /**
       * 缓冲区滚过去了：发一帧 gap 然后结束订阅（M3 §7.3 —— gap 之后订阅立即停止）。
       *
       * **必须用 res.end() 优雅关闭，不能 socket.destroy()。**
       * destroy() 是立刻销毁套接字、不 flush 缓冲，前面那几帧会和 gap 帧一起
       * 被丢掉——客户端收到的是"连接被重置"，一帧都没有，于是根本走不到
       * gap 恢复那条路径（实测就是这个症状）。end() 会先把已写的数据刷出去。
       */
      res.write(gapFrame("3"));
      res.end();
      return;
    }
    if (ending === "truncate") {
      res.socket?.destroy();
      return;
    }
    res.write(sseFrame("end", null));
    res.end();
  }

  /* ------------------------------ JSON fixture ------------------------------ */

  /** 非 SSE 的 /api 请求 → 手写 JSON。返回 null 表示这个端点没 mock 到 */
  jsonFixture(method: string, pathname: string): { status: number; body: unknown } | null {
    const threadId = /\/threads\/([^/]+)/.exec(pathname)?.[1] ?? THREAD_ID;
    const fixture = this.fixture(threadId);

    if (pathname === "/api/threads" && method === "POST") {
      return { status: 200, body: this.threadBody(threadId) };
    }
    if (pathname === "/api/threads/search" || pathname === "/api/threads/search/") {
      return { status: 200, body: { data: [], next_cursor: null, has_more: false } };
    }
    // 侧栏走的是 GET /api/threads（不是 SDK 的 /threads/search）：
    // 网关的 ThreadSearchRequest 是 extra="forbid"，SDK 发的 offset 会被 422。
    if (pathname === "/api/threads" && method === "GET") {
      return {
        status: 200,
        body: { data: [this.threadBody(threadId)], next_cursor: null, has_more: false },
      };
    }
    // 模型目录：设置面板用
    if (pathname === "/api/models") {
      return {
        status: 200,
        body: {
          models: [
            {
              name: "deepseek-v4-flash",
              model: "deepseek-v4-flash",
              display_name: "DeepSeek V4",
              description: null,
              supports_thinking: true,
              supports_reasoning_effort: false,
              supports_vision: false,
              context_window: 128000,
            },
          ],
        },
      };
    }
    // ---- 认证（工作区外壳进入时必发，见 AUTH_USER 的注释）----
    if (pathname === "/api/v1/auth/me") {
      return { status: 200, body: AUTH_USER };
    }
    if (pathname === "/api/v1/auth/providers") {
      return { status: 200, body: { local: true } };
    }
    if (pathname === "/api/v1/auth/logout") {
      return { status: 200, body: { ok: true } };
    }

    if (pathname === "/api/info") return { status: 200, body: {} };
    if (pathname === "/api/assistants" || pathname === "/api/assistants/search") {
      return { status: 200, body: [] };
    }
    if (pathname === "/api/threads/" + threadId) {
      return { status: 200, body: this.threadBody(threadId) };
    }
    if (pathname === "/api/threads/" + threadId + "/state") {
      if (method === "GET") this.stateReloadCount += 1;
      return { status: 200, body: this.stateBody(threadId) };
    }
    if (pathname === "/api/threads/" + threadId + "/history") {
      return { status: 200, body: [] };
    }
    if (pathname === "/api/threads/" + threadId + "/messages") {
      /**
       * 网关形状：Page[MessageResponse]（见 src/core/api/messages-feed.ts）。
       *
       * **不是 LangChain 消息**：账本行只有
       * {thread_id, run_id, seq, event_type, content, metadata, created_at}。
       * fixture.messages 那一份是 LangChain 形状（/state 要用），所以这里转换。
       */
      return {
        status: 200,
        body: {
          data: fixture.messages.map((m) => toLedgerRow(m, threadId)),
          next_cursor: null,
          has_more: false,
        },
      };
    }
    if (pathname === "/api/threads/" + threadId + "/runs") {
      return { status: 200, body: fixture.runningRunId ? [this.runBody(threadId, "running")] : [] };
    }
    if (/^\/api\/threads\/[^/]+\/runs\/[^/]+\/cancel$/.test(pathname)) {
      // §5.1：停止走这里（SDK 的 cancel 是 POST ...?action=interrupt）
      this.cancelCount += 1;
      fixture.cancelled = true;
      fixture.runningRunId = null;
      for (const held of this.held.splice(0)) {
        if (!held.writableEnded && !held.destroyed) {
          held.write(sseFrame("end", null));
          held.end();
        }
      }
      return { status: 200, body: this.runBody(threadId, "interrupted") };
    }
    if (/^\/api\/threads\/[^/]+\/runs\/[^/]+$/.test(pathname) && method === "GET") {
      return {
        status: 200,
        body: this.runBody(threadId, fixture.runningRunId ? "running" : "success"),
      };
    }
    return null;
  }

  private threadBody(threadId: string): unknown {
    return {
      thread_id: threadId,
      display_name: this.fixture(threadId).displayName,
      created_at: "2026-09-16T00:00:00Z",
      updated_at: "2026-09-16T00:00:00Z",
      status: "idle",
      metadata: {},
      values: {},
    };
  }

  private runBody(threadId: string, status: string): unknown {
    return {
      run_id: RUN_ID,
      thread_id: threadId,
      assistant_id: "agent",
      status: status,
      created_at: "2026-09-16T00:00:00Z",
      updated_at: "2026-09-16T00:00:00Z",
      metadata: {},
      kwargs: {},
      multitask_strategy: "reject",
    };
  }

  private stateBody(threadId: string): unknown {
    return {
      values: {
        messages: this.fixture(threadId).messages,
        artifacts: [],
        todos: [],
        title: "E2E 会话",
      },
      next: [],
      tasks: [],
      metadata: {},
      created_at: "2026-09-16T00:00:00Z",
      checkpoint: {
        thread_id: threadId,
        checkpoint_ns: "",
        checkpoint_id: "cp-e2e-1",
        checkpoint_map: {},
      },
      parent_checkpoint: null,
    };
  }
}

/* -------------------------------------------------------------------------- */
/* 网络拦截                                                                    */
/* -------------------------------------------------------------------------- */

/**
 * 拦住所有 /api 请求。
 *
 * - 非 SSE：route.fulfill 返回手写 JSON fixture（mock 不到的端点显式 501，不静默放过）
 * - SSE：route.continue 转发到本地 mock gateway（原因见文件头第二节）
 */
async function interceptApi(page: Page, gateway: MockGateway): Promise<void> {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const method = request.method();

    if (method === "OPTIONS") {
      await route.fulfill({ status: 204, headers: CORS_HEADERS });
      return;
    }

    if (gateway.isStreamPath(url.pathname)) {
      await route.continue({ url: gateway.originFor(url.pathname, url.search) });
      return;
    }

    const fixture = gateway.jsonFixture(method, url.pathname);
    if (!fixture) {
      // 没 mock 到的端点显式失败：静默 200 会让 UI 的问题变成"页面空白"。
      // 用例最后用 expect(backend.unhandled).toEqual([]) 把它暴露出来。
      gateway.unhandled.push(method + " " + url.pathname);
      await route.fulfill({
        status: 501,
        headers: { "Content-Type": "application/json", ...CORS_HEADERS },
        body: JSON.stringify({ error: "unmocked endpoint: " + method + " " + url.pathname }),
      });
      return;
    }
    await route.fulfill({
      status: fixture.status,
      headers: { "Content-Type": "application/json", ...CORS_HEADERS },
      body: JSON.stringify(fixture.body),
    });
  });
}

/* -------------------------------------------------------------------------- */
/* fixtures / helpers                                                          */
/* -------------------------------------------------------------------------- */

const test = base.extend<{ backend: MockGateway }>({
  // 第二个参数按位置传，名字随便取——不叫 use 是为了避开
  // eslint-config-next 里 react-hooks 规则对 "use*" 的误判。
  backend: async ({}, provide) => {
    const gateway = new MockGateway();
    await gateway.start();
    try {
      await provide(gateway);
    } finally {
      await gateway.close();
    }
  },
});

/* ---------------------------- 页面定位 ---------------------------- */
/* 全部走可访问名（role + name）而不是 testid：这些名字本身就是聊天页
   对外的语义接口，改文案时重构会立刻在这里反映出来。 */

const composerInput = (page: Page) => page.getByRole("textbox", { name: "消息输入框" });
const sendButton = (page: Page) => page.getByRole("button", { name: "发送" });
const stopButton = (page: Page) => page.getByRole("button", { name: "停止" });
/** StepsPanel：流式期间触发器写"处理中…"，结束后写"已完成" */
const stepsPanel = (page: Page) => page.getByRole("region", { name: "处理过程" });
/** 工具行：折叠时只有一行，参数和结果要点开才挂载（§9 验收清单第一条） */
const toolRow = (page: Page, toolName: string) =>
  page.getByRole("region", { name: "工具调用 " + toolName });
const toolToggle = (page: Page, toolName: string) =>
  page.getByRole("button", { name: new RegExp("工具 " + toolName) });
const humanBubbles = (page: Page) => page.getByRole("article", { name: "用户消息" });
const answerBubble = (page: Page) => page.getByRole("article", { name: "助手回复" });

async function openChat(page: Page, backend: MockGateway): Promise<void> {
  await interceptApi(page, backend);
  await page.goto("/workspace/chats/" + backend.threadId);
  await expect(composerInput(page)).toBeVisible();
}

async function sendMessage(page: Page, text: string): Promise<void> {
  await composerInput(page).fill(text);
  await sendButton(page).click();
}

/* -------------------------------------------------------------------------- */
/* 用例                                                                        */
/* -------------------------------------------------------------------------- */

test("发送一条消息：SSE 流式返回，文字逐步出现", async ({ page, backend }) => {
  backend.setPlan({
    frames: [
      { text: metadataFrame(backend.runId, backend.threadId) },
      { text: messagesFrame(humanMessage("你好", 1), 1) },
      // gate：mock 停在这里等测试放行。这样"先出现第一段"是可断言的，不是碰运气
      { text: messagesFrame(aiChunk("第一", "ai-1"), 2), gate: "chunk-1" },
      { text: messagesFrame(aiChunk("段答案", "ai-1"), 3), gate: "chunk-2" },
    ],
    ending: "end",
  });

  await openChat(page, backend);
  await sendMessage(page, "你好");

  // 临时诊断
  await page.waitForTimeout(1500);
  await expect(page.getByText("正在生成…").first()).toBeVisible();

  // 第一帧到达之前不会有步骤面板 —— 空内容的 AI 消息不产生分组，
  // 所以"处理中…"是跟着第一段文字一起出现的，不能提前断言。
  backend.release("chunk-1");
  await expect(stepsPanel(page)).toContainText("处理中…");
  await expect(stepsPanel(page)).toContainText("第一");
  // 第二段还没到：证明是一帧一帧地渲染，不是整包
  await expect(stepsPanel(page)).not.toContainText("段答案");

  // chunk-2 之后 mock 紧接着发 end，所以这一条要断言在**终态气泡**上：
  // 流一结束，同一条消息就从"处理中"分组移到"已完成"气泡（§6.1/§6.2），
  // 这正是要验证的行为，而不是它还留在步骤面板里。
  backend.release("chunk-2");

  await expect(answerBubble(page)).toContainText("第一段答案");
  await expect(page.getByText("就绪")).toBeVisible();

  // 只开了一个 run：发一次消息不会重复建流
  expect(backend.createCount).toBe(1);
  expect(backend.unhandled).toEqual([]);
});

test("首轮完成：侧栏标题自动更新，不需要刷新页面", async ({ page, backend }) => {
  backend.setPlan({
    frames: [
      { text: metadataFrame(backend.runId, backend.threadId) },
      { text: messagesFrame(humanMessage("你好", 1), 1) },
      { text: messagesFrame(aiChunk("你好！", "ai-1"), 2) },
    ],
    ending: "end",
  });

  await openChat(page, backend);
  const recentThreads = page.getByRole("navigation", { name: "最近对话" });
  await expect(recentThreads.getByText("新对话", { exact: true })).toBeVisible();

  /**
   * 模拟 worker 收尾：真实后端在发 end 帧之前把 checkpoint 的 title 投影成
   * threads_meta.display_name。列表已在首屏缓存了 "新对话"，前端必须主动失效
   * ["threads"] 才会重新拉到这个名字；否则用户得刷新页面。
   */
  backend.setThreadTitle("问候");
  await sendMessage(page, "你好");

  await expect(answerBubble(page)).toContainText("你好！");
  await expect(recentThreads.getByText("问候", { exact: true })).toBeVisible();
  expect(backend.unhandled).toEqual([]);
});

test("带工具调用的多轮：工具行点开显示参数和结果", async ({ page, backend }) => {
  const args = { path: "notes.txt", content: "hello" };
  backend.setPlan({
    frames: [
      { text: metadataFrame(backend.runId, backend.threadId) },
      { text: messagesFrame(humanMessage("帮我写个文件", 1), 1) },
      {
        text: messagesFrame(
          aiToolCallMessage("ai-tool", "我先写一个文件。", "call-1", "write_file", args),
          2,
        ),
        gate: "after-tool-call",
      },
      {
        text: messagesFrame(
          toolResultMessage("tool-1", "call-1", "write_file", "wrote 11 bytes to notes.txt"),
          3,
        ),
      },
      { text: messagesFrame(aiChunk("写好了，notes.txt 里是 hello。", "ai-2"), 4) },
    ],
    ending: "end",
  });

  await openChat(page, backend);
  await sendMessage(page, "帮我写个文件");

  backend.release("after-tool-call");
  const row = toolRow(page, "write_file");
  const toggle = toolToggle(page, "write_file");
  await expect(row).toBeVisible();

  // 折叠时一行读完：工具名、参数摘要、状态（状态在无障碍名里，免得占行宽）
  await expect(page.getByRole("button", { name: "工具 write_file，成功" })).toBeVisible();
  await expect(toggle).toContainText("write_file");
  await expect(toggle).toContainText("notes.txt");
  // 结果还没点开就不进 DOM —— 但点开必须有（§9 验收清单第一条不是"删掉细节"）
  await expect(row).not.toContainText("wrote 11 bytes");

  await toggle.click();
  await expect(row).toContainText("参数");
  await expect(row).toContainText("hello");
  await expect(row).toContainText("结果");
  await expect(row).toContainText("wrote 11 bytes");

  // §6.1：带 tool_calls 的 AI 消息**同时**有可见正文，那段正文不能被吞掉
  await expect(stepsPanel(page)).toContainText("我先写一个文件。");

  // 这一轮的收尾文本落到终态气泡里
  await expect(answerBubble(page)).toContainText("写好了");
  expect(backend.unhandled).toEqual([]);
});

test("刷新页面后对话继续流式，而不是重跑一次", async ({ page, backend }) => {
  backend.setPlan({
    frames: [
      { text: metadataFrame(backend.runId, backend.threadId) },
      { text: messagesFrame(humanMessage("帮我数一下", 1), 1) },
      { text: messagesFrame(aiChunk("正在数", "ai-1"), 2), gate: "before-reload" },
    ],
    // run 还在跑：创建端的连接一直挂着，不发 end
    ending: "hold",
    resumeFrames: [
      { text: messagesFrame(aiChunk("……数完了。", "ai-1"), 3), gate: "after-reload" },
    ],
    resumeEnding: "end",
  });
  backend.markRunRunning();

  await openChat(page, backend);
  await sendMessage(page, "帮我数一下");

  backend.release("before-reload");
  await expect(stepsPanel(page)).toContainText("处理中…");
  await expect(stepsPanel(page)).toContainText("正在数");

  const createsBeforeReload = backend.createCount;
  expect(createsBeforeReload).toBe(1);

  // 这一轮在刷新前落进了持久化历史（真实情况下是 M4 的 feed）
  backend.seedMessages(backend.threadId, [
    humanMessage("帮我数一下", 1),
    { type: "ai", id: "ai-1", content: "正在数", seq: 2, run_id: backend.runId },
  ]);

  await page.reload();
  await expect(composerInput(page)).toBeVisible();

  // ★ 最关键的一条：刷新是**订阅还在跑的 run**，不是重新发一次消息（§5.1）
  await expect.poll(() => backend.joinCount).toBeGreaterThan(0);
  expect(backend.createCount).toBe(createsBeforeReload);

  // 续上的那一段也要真的流出来
  backend.release("after-reload");
  await expect(page.getByText("数完了")).toBeVisible();

  // 同一条用户消息只出现一次：刷新没有把这一轮重放成两条（§4.2 的身份锚点）
  await expect(humanBubbles(page)).toHaveCount(1);
  expect(backend.unhandled).toEqual([]);
});

test("手动截断 SSE：出现恢复提示，且不显示已完成", async ({ page, backend }) => {
  backend.setPlan({
    frames: [
      { text: metadataFrame(backend.runId, backend.threadId) },
      { text: messagesFrame(humanMessage("讲个长故事", 1), 1) },
      { text: messagesFrame(aiChunk("从前有座山", "ai-1"), 2) },
    ],
    // gap 帧之后掐断连接：模拟 Gateway 重启 / 缓冲区已经滚过去（M3 §7.3）
    ending: "gap",
    resumeFrames: [
      { text: messagesFrame(aiChunk("，山里有座庙。", "ai-1"), 3), gate: "after-gap" },
    ],
    resumeEnding: "end",
  });

  await openChat(page, backend);
  await sendMessage(page, "讲个长故事");
  await expect(stepsPanel(page)).toContainText("从前有座山");

  // ★ gap 绝不能表现为"正常流结束"：界面显示恢复提示，而不是"已完成"（§5.2）
  await expect(page.getByText("连接中断，正在恢复…")).toBeVisible();
  await expect(page.getByText("已完成")).toHaveCount(0);
  await expect(stepsPanel(page)).toContainText("处理中…");
  await expect(page.getByText("就绪")).toHaveCount(0);

  // §5.2 第 3 条：恢复时要先用 values 重载持久化状态
  await expect.poll(() => backend.stateReloadCount).toBeGreaterThan(0);
  // §5.2 第 4 条：从服务端给的保留尾部之后续订
  await expect.poll(() => backend.joinCount).toBeGreaterThan(0);

  backend.release("after-gap");
  await expect(page.getByText("山里有座庙")).toBeVisible();
  // 这时候才允许出现终态
  await expect(answerBubble(page)).toContainText("山里有座庙");
  expect(backend.unhandled).toEqual([]);
});

test("点击停止：cancel 请求发出，按钮状态正确", async ({ page, backend }) => {
  backend.setPlan({
    frames: [
      { text: metadataFrame(backend.runId, backend.threadId) },
      { text: messagesFrame(humanMessage("写一篇长的", 1), 1) },
      { text: messagesFrame(aiChunk("写啊写……", "ai-1"), 2) },
    ],
    ending: "hold",
  });
  backend.markRunRunning();

  await openChat(page, backend);
  await sendMessage(page, "写一篇长的");

  // 流式期间发送按钮变成停止按钮（§5.1）
  await expect(stopButton(page)).toBeVisible();
  await expect(stepsPanel(page)).toContainText("写啊写……");

  await stopButton(page).click();

  // POST /threads/{tid}/runs/{rid}/cancel 真的发出去了
  await expect.poll(() => backend.cancelCount).toBe(1);
  // 按钮回到"发送"，输入框还能用（不是永久卡在停止中）
  await expect(sendButton(page)).toBeVisible();
  await expect(stopButton(page)).toHaveCount(0);
  await expect(composerInput(page)).toBeEnabled();
  expect(backend.unhandled).toEqual([]);
});