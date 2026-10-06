#!/usr/bin/env node
/**
 * 本地按"生产形态"启动前端。
 *
 * 为什么要这个脚本，而不是直接用 next start：
 *
 * next.config.ts 设了 output: "standalone"（docker/Dockerfile 的前提）。
 * 这个模式下 Next 会明确警告 "next start" does not work with "output: standalone"，
 * 而且 standalone 产物**不含 .next/static 和 public/**——不拷过去的话，
 * 页面能返回 200，但所有 JS/CSS 都是 404，页面白屏且控制台一堆 404。
 *
 * docker/Dockerfile 里就是手写这几步 COPY 的。这个脚本是它的本地等价物，
 * 让"本机跑生产形态"和"容器里跑"走同一条路径。
 *
 *   pnpm build && pnpm start:prod
 */
import { cpSync, existsSync, rmSync } from "node:fs";
import { spawn } from "node:child_process";
import path from "node:path";
import process from "node:process";

const root = process.cwd();
const standalone = path.join(root, ".next", "standalone");
const serverEntry = path.join(standalone, "server.js");

if (!existsSync(serverEntry)) {
  console.error(
    "找不到 .next/standalone/server.js —— 先跑 pnpm build。\n" +
      "（另外：跑过 pnpm dev 之后 .next 会被开发产物覆盖，需要重新 build。）",
  );
  process.exit(1);
}

// 1) 静态资源：standalone 不会带它们
const staticSrc = path.join(root, ".next", "static");
const staticDest = path.join(standalone, ".next", "static");
if (existsSync(staticSrc)) {
  rmSync(staticDest, { recursive: true, force: true });
  cpSync(staticSrc, staticDest, { recursive: true });
} else {
  console.warn("[start-standalone] 没有 .next/static，页面会白屏。先 pnpm build。");
}

// 2) public/：同上（仓库目前没有这一层，有的话就拷）
const publicSrc = path.join(root, "public");
if (existsSync(publicSrc)) {
  cpSync(publicSrc, path.join(standalone, "public"), { recursive: true });
}

const port = process.env.PORT ?? "3000";
const host = process.env.HOSTNAME ?? "127.0.0.1";

console.log("[start-standalone] http://" + host + ":" + port);

const child = spawn(process.execPath, [serverEntry], {
  cwd: root,
  stdio: "inherit",
  env: { ...process.env, PORT: port, HOSTNAME: host, NODE_ENV: "production" },
});
child.on("exit", (code) => process.exit(code ?? 0));
