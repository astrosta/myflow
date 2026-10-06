import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

// 手写的脚本没有类型声明（也不打算加 d.ts）：vitest 直接跑 .mjs，
// tsc 这一侧用 @ts-expect-error 说明白。
import {
  analyzeComposeText,
  checkBindAddress,
  checkComposeFile,
  defaultComposeFiles,
  parseComposePorts,
  runCheck,
  // @ts-expect-error check-compose-ports.mjs 没有类型声明（错误报在模块说明符这一行）
} from "./check-compose-ports.mjs";

/**
 * M5 §7.3：**每一条 published port 都必须带显式的绑定地址**。
 *
 * 这一组测试存在的理由就是文档里那句话：两种写法只差几个字符，
 * 肉眼 review 时极容易漏。所以先把四种基本写法焊死，再拿真实 compose 文件跑一遍。
 */

const FRONTEND_ROOT = fileURLToPath(new URL("..", import.meta.url));
const REAL_COMPOSE = fileURLToPath(new URL("../docker/docker-compose.yml", import.meta.url));

function doc(lines: string[]): string {
  return lines.join("\n") + "\n";
}

/** 造一个最小的 compose：一个 service，ports 用短语法块序列 */
function shortForm(...entries: string[]): string {
  return doc([
    "services:",
    "  app:",
    "    image: alpine:3.20",
    "    ports:",
    ...entries.map((entry) => "      - " + entry),
  ]);
}

/** 造一个最小的 compose：ports 用 flow 序列 */
function flowForm(...entries: string[]): string {
  return doc([
    "services:",
    "  app:",
    "    image: alpine:3.20",
    "    ports: [" + entries.join(", ") + "]",
  ]);
}

function violationsOf(text: string) {
  return analyzeComposeText(text, "fixture.yml").violations;
}

describe("短语法", () => {
  it("裸 2026:2026 判为违规（等价于绑 0.0.0.0）", () => {
    const violations = violationsOf(shortForm('"2026:2026"'));
    expect(violations).toHaveLength(1);
    expect(violations[0].raw).toContain("2026:2026");
    expect(violations[0].line).toBe(5);
    expect(violations[0].reason).toContain("0.0.0.0");
    expect(violations[0].fix).toBe('"127.0.0.1:2026:2026"');
  });

  it('"${PORT:-2026}:2026" 判为违规（文档点名的那个坑）', () => {
    const violations = violationsOf(shortForm('"${PORT:-2026}:2026"'));
    expect(violations).toHaveLength(1);
    expect(violations[0].raw).toContain("PORT:-2026");
    expect(violations[0].raw).toContain(":2026");
    expect(violations[0].reason).toContain("没有绑定地址");
  });

  it("flow 序列里的裸 ${PORT:-2026}:2026 同样判为违规", () => {
    const violations = violationsOf(flowForm('"${PORT:-2026}:2026"'));
    expect(violations).toHaveLength(1);
  });

  it("只写容器端口（8001）也判为违规", () => {
    const violations = violationsOf(shortForm('"8001"'));
    expect(violations).toHaveLength(1);
    expect(violations[0].reason).toContain("只写了容器端口");
  });

  it('"127.0.0.1:2026:2026" 通过', () => {
    const result = analyzeComposeText(shortForm('"127.0.0.1:2026:2026"'), "fixture.yml");
    expect(result.violations).toEqual([]);
    expect(result.checked).toHaveLength(1);
    expect(result.checked[0].bind).toBe("127.0.0.1");
  });

  it('"${BIND_HOST:-127.0.0.1}:${PORT:-2026}:2026" 通过（compose 里用的就是这条）', () => {
    const result = analyzeComposeText(
      shortForm('"${BIND_HOST:-127.0.0.1}:${PORT:-2026}:2026"'),
      "fixture.yml",
    );
    expect(result.violations).toEqual([]);
    expect(result.checked).toHaveLength(1);
    expect(result.checked[0].bind).toBe("${BIND_HOST:-127.0.0.1}");
  });

  it("协议后缀 /udp、IPv6 字面量都能通过", () => {
    expect(violationsOf(shortForm('"127.0.0.1:2026:2026/udp"'))).toEqual([]);
    expect(violationsOf(shortForm('"[::1]:2026:2026"'))).toEqual([]);
  });

  it("主机名当绑定地址判为违规（compose 只接受 IP）", () => {
    const violations = violationsOf(shortForm('"localhost:2026:2026"'));
    expect(violations).toHaveLength(1);
    expect(violations[0].reason).toContain("主机名");
  });

  it("默认值为空的插值判为违规（${BIND_HOST:-}）", () => {
    const violations = violationsOf(shortForm('"${BIND_HOST:-}:2026:2026"'));
    expect(violations).toHaveLength(1);
    expect(violations[0].reason).toContain("默认值为空");
  });

  it("多行 ports（多个条目）逐条判定", () => {
    const violations = violationsOf(
      shortForm('"127.0.0.1:2026:2026"', '"2026:2026"', '"[::1]:9000:9000"'),
    );
    expect(violations).toHaveLength(1);
    expect(violations[0].line).toBe(6);
  });
});

describe("长语法", () => {
  it("缺 host_ip 判为违规（块映射写法）", () => {
    const violations = violationsOf(
      doc([
        "services:",
        "  app:",
        "    ports:",
        "      - target: 2026",
        "        published: 2026",
      ]),
    );
    expect(violations).toHaveLength(1);
    expect(violations[0].line).toBe(4);
    expect(violations[0].reason).toContain("host_ip");
    expect(violations[0].fix).toContain("127.0.0.1");
  });

  it("缺 host_ip 判为违规（flow 映射写法）", () => {
    const violations = violationsOf(
      doc(["services:", "  app:", "    ports: [{ target: 2026, published: 2026 }]"]),
    );
    expect(violations).toHaveLength(1);
    expect(violations[0].reason).toContain("host_ip");
  });

  it("有 host_ip 通过（块映射 + flow 映射）", () => {
    const block = analyzeComposeText(
      doc([
        "services:",
        "  app:",
        "    ports:",
        "      - target: 2026",
        "        published: 2026",
        '        host_ip: "127.0.0.1"',
      ]),
      "fixture.yml",
    );
    expect(block.violations).toEqual([]);
    expect(block.checked).toHaveLength(1);
    expect(block.checked[0].bind).toBe("127.0.0.1");

    const flow = analyzeComposeText(
      doc(["services:", "  app:", '    ports: [{ target: 2026, published: 2026, host_ip: "127.0.0.1" }]']),
      "fixture.yml",
    );
    expect(flow.violations).toEqual([]);
    expect(flow.checked).toHaveLength(1);
  });

  it("只写 target、没有 published：不是发布端口，不算违规", () => {
    expect(violationsOf(doc(["services:", "  app:", "    ports:", "      - target: 8001"]))).toEqual([]);
  });

  it("host_ip 是 0.0.0.0 也算显式（是有意为之，不是漏写）", () => {
    const result = analyzeComposeText(
      doc(["services:", "  app:", '    ports: ["0.0.0.0:2026:2026"]']),
      "fixture.yml",
    );
    expect(result.violations).toEqual([]);
    expect(result.checked[0].bind).toBe("0.0.0.0");
  });

  it("不认识的键报无法解析，不静默放过", () => {
    const violations = violationsOf(
      doc(["services:", "  app:", "    ports:", "      - target: 2026", "        hostPort: 2026"]),
    );
    expect(violations.length).toBeGreaterThan(0);
  });
});

describe("解析不确定性：宁可报错也不放过", () => {
  it("ports 值写成字符串（YAML 锚点之类）报无法解析", () => {
    const violations = violationsOf(doc(["services:", "  app:", "    ports: *shared-ports"]));
    expect(violations).toHaveLength(1);
    expect(violations[0].reason).toContain("无法解析");
  });

  it("flow 序列缺少收尾 ] 报无法解析", () => {
    const violations = violationsOf(doc(["services:", "  app:", '    ports: ["2026:2026"']));
    expect(violations).toHaveLength(1);
    expect(violations[0].reason).toContain("无法解析");
  });

  it("冒号超过 3 段（未加方括号的 IPv6）报无法解析", () => {
    const violations = violationsOf(shortForm('"::1:2026:2026"'));
    expect(violations).toHaveLength(1);
  });

  it("违规信息带文件、行号、原始条目和修复建议", () => {
    const violations = analyzeComposeText(shortForm('"2026:2026"'), "some/dir/compose.yml").violations;
    expect(violations).toHaveLength(1);
    const item = violations[0];
    expect(item.file).toBe("some/dir/compose.yml");
    expect(item.line).toBe(5);
    expect(item.raw).toContain("2026:2026");
    expect(item.service).toBe("app");
    expect(item.fix).toBeTruthy();
  });

  it("checkBindAddress 的边界：空、越界 IPv4、不完整 IPv6", () => {
    expect(checkBindAddress("").ok).toBe(false);
    expect(checkBindAddress("999.1.1.1").ok).toBe(false);
    expect(checkBindAddress("[::1").ok).toBe(false);
    expect(checkBindAddress("127.0.0.1").ok).toBe(true);
    expect(checkBindAddress("[::]").ok).toBe(true);
    expect(checkBindAddress("${BIND_HOST}").ok).toBe(true);
  });

  it("多 service 的文件每个 service 各自检查", () => {
    const text = doc([
      "services:",
      "  good:",
      "    ports:",
      '      - "127.0.0.1:3000:3000"',
      "  bad:",
      "    ports:",
      '      - "3001:3001"',
    ]);
    const parsed = parseComposePorts(text);
    expect(parsed.services.map((service: { name: string }) => service.name)).toEqual(["good", "bad"]);
    const violations = violationsOf(text);
    expect(violations).toHaveLength(1);
    expect(violations[0].service).toBe("bad");
  });
});

describe("真实的 frontend/docker/docker-compose.yml", () => {
  it("没有任何违规", () => {
    const report = checkComposeFile(REAL_COMPOSE);
    expect(report.violations).toEqual([]);
  });

  it("确实扫到了那条 published port（不是空扫）", () => {
    const report = checkComposeFile(REAL_COMPOSE);
    expect(report.checked).toHaveLength(1);
    expect(report.checked[0].service).toBe("frontend");
    expect(report.checked[0].raw).toContain("${BIND_HOST:-127.0.0.1}");
    expect(report.checked[0].raw).toContain("${PORT:-2026}:2026");
  });

  it("gateway 的 8001 只在 expose 里，没有出现在 ports 里", () => {
    const report = checkComposeFile(REAL_COMPOSE);
    const gateway = report.services.find((service: { name: string }) => service.name === "myflow-gateway");
    expect(gateway).toBeTruthy();
    expect(gateway.ports).toEqual([]);
  });

  it("默认扫描集合包含 frontend/docker/docker-compose.yml", () => {
    const files = defaultComposeFiles();
    expect(files.some((file: string) => file.endsWith("docker-compose.yml"))).toBe(true);
  });

  it("runCheck 对真实文件返回 ok", () => {
    const result = runCheck([REAL_COMPOSE]);
    expect(result.ok).toBe(true);
    expect(result.violations).toEqual([]);
  });

  it("扫不到文件时是失败，不是静默通过", () => {
    const result = runCheck([]);
    expect(result.ok).toBe(false);
  });
});
