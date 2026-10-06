#!/usr/bin/env node
/**
 * 扫 compose 文件里所有 published port，断言**每一条都带显式的绑定地址**（M5 §7.3）。
 *
 *   ports: ["${PORT:-2026}:2026"]                        # ✗ 绑 0.0.0.0，局域网都能访问
 *   ports: ["${BIND_HOST:-127.0.0.1}:${PORT:-2026}:2026"]  # ✓
 *
 * 这两种写法肉眼只差几个字符，review 时几乎必然漏掉，所以用机器扫。
 *
 * 用法（cwd 无所谓，脚本按自己的位置定位仓库根）：
 *   node scripts/check-compose-ports.mjs                 # 扫默认集合
 *   node scripts/check-compose-ports.mjs path/a.yml ...  # 只扫指定文件
 *
 * 默认集合 = 仓库根下的 compose*.yml / docker-compose*.yml
 *          + frontend/ 与 frontend/docker/ 下的同名文件。
 *
 * 覆盖两种语法：
 *   短语法  "127.0.0.1:2026:2026" / "127.0.0.1:2026:2026/udp" / "[::1]:2026:2026"
 *   长语法  { target: 2026, published: 2026, host_ip: "127.0.0.1" }
 *           **长语法缺 host_ip 同样算违规**（等价于绑 0.0.0.0）。
 *
 * 解析是手写的（不引依赖），只处理 compose 需要的缩进结构。**解析不确定的地方
 * 一律报"无法解析"，绝不静默放过** —— 这个脚本存在的全部意义就是"漏报比误报危险"。
 */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
export const FRONTEND_DIR = path.resolve(SCRIPT_DIR, "..");
export const REPO_ROOT = path.resolve(FRONTEND_DIR, "..");

const COMPOSE_FILE_RE = /^(?:docker-)?compose(?:\.[A-Za-z0-9_.-]+)?\.ya?ml$/i;
const LONG_SYNTAX_KEYS = new Set([
  "target",
  "published",
  "host_ip",
  "protocol",
  "mode",
  "name",
  "app_protocol",
]);

/* -------------------------------------------------------------------------- */
/* 文本工具                                                                    */
/* -------------------------------------------------------------------------- */

/** 去掉行尾注释（# 要在引号外、且前面是空白才当注释）。返回剩下的原文。 */
export function stripComment(line) {
  let out = "";
  let quote = null;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (quote) {
      out += ch;
      if (ch === "\\" && quote === '"') {
        out += line[i + 1] || "";
        i += 1;
        continue;
      }
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      out += ch;
      continue;
    }
    if (ch === "#" && (i === 0 || /\s/.test(line[i - 1]))) break;
    out += ch;
  }
  return out;
}

/** 去掉一层配对引号 */
function unquote(text) {
  const trimmed = text.trim();
  if (trimmed.length >= 2) {
    const first = trimmed[0];
    const last = trimmed[trimmed.length - 1];
    if ((first === '"' || first === "'") && first === last) return trimmed.slice(1, -1);
  }
  return trimmed;
}

/** 按顶层逗号切分 flow 序列/映射的内容（尊重引号与嵌套括号） */
export function splitFlowItems(inner) {
  const items = [];
  let depth = 0;
  let quote = null;
  let current = "";
  for (const ch of inner) {
    if (quote) {
      current += ch;
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      current += ch;
      continue;
    }
    if (ch === "{" || ch === "[") depth += 1;
    if (ch === "}" || ch === "]") depth -= 1;
    if (ch === "," && depth === 0) {
      items.push(current);
      current = "";
      continue;
    }
    current += ch;
  }
  if (current.trim()) items.push(current);
  return items.map((item) => item.trim()).filter((item) => item.length > 0);
}

/** 解析 flow 映射内容（不含外层花括号）成对象；解析不了返回 null */
export function parseInlineMap(inner) {
  const map = {};
  for (const item of splitFlowItems(inner)) {
    const idx = item.indexOf(":");
    if (idx <= 0) return null;
    map[unquote(item.slice(0, idx))] = unquote(item.slice(idx + 1));
  }
  return map;
}

/* -------------------------------------------------------------------------- */
/* 绑定地址 / 短语法 / 长语法判定                                                */
/* -------------------------------------------------------------------------- */

const INTERPOLATION_RE = /^(\$\{[A-Za-z_][A-Za-z0-9_]*(?::[-+?][^}]*)?\}|\$[A-Za-z_][A-Za-z0-9_]*)$/;

/**
 * 绑定地址必须是一个**显式的**东西：
 *   - IP 字面量（v4 / 带方括号的 v6）
 *   - compose 变量插值（${BIND_HOST:-127.0.0.1} 或 ${BIND_HOST}）
 * 主机名不接受：compose 的绑定地址只认 IP，"localhost" 会表现为另一种（可能更糟的）结果。
 */
export function checkBindAddress(raw) {
  const value = unquote(raw);
  if (!value) return { ok: false, reason: "绑定地址为空" };

  if (INTERPOLATION_RE.test(value)) {
    const defaultMatch = /:[-+?]([^}]*)\}$/.exec(value);
    if (defaultMatch && defaultMatch[1].trim() === "") {
      return {
        ok: false,
        reason: "变量插值的默认值为空（" + value + "），展开后会退化成没有绑定地址",
      };
    }
    return { ok: true, value: value };
  }
  if (/^\[[0-9A-Fa-f:.]+\]$/.test(value)) return { ok: true, value: value };
  if (/^\d{1,3}(?:\.\d{1,3}){3}$/.test(value)) {
    const octets = value.split(".").map(Number);
    if (octets.every((octet) => octet >= 0 && octet <= 255)) return { ok: true, value: value };
    return { ok: false, reason: "IPv4 字面量越界：" + value };
  }
  if (value.startsWith("[")) return { ok: false, reason: "IPv6 字面量不完整：" + value };
  if (/^[A-Za-z0-9_.-]+$/.test(value)) {
    return {
      ok: false,
      reason: '"' + value + '" 看起来是主机名；绑定地址只接受 IP 字面量或变量插值',
    };
  }
  return { ok: false, reason: "无法识别的绑定地址：" + value };
}

/**
 * 按顶层冒号切分（跳过 ${...} 插值内部的冒号）。
 *
 * 不能直接用 split(":")：${BIND_HOST:-127.0.0.1}:${PORT:-2026}:2026 里
 * 的 ":-" 是插值语法，不是端口分隔符。
 */
export function splitHostPortSegments(value) {
  const segments = [];
  let current = "";
  let depth = 0;
  for (let i = 0; i < value.length; i += 1) {
    const ch = value[i];
    if (ch === "$" && value[i + 1] === "{") {
      depth += 1;
      current += "${";
      i += 1;
      continue;
    }
    if (depth > 0 && ch === "}") {
      depth -= 1;
      current += ch;
      continue;
    }
    if (ch === ":" && depth === 0) {
      segments.push(current);
      current = "";
      continue;
    }
    current += ch;
  }
  segments.push(current);
  return segments;
}

/** 短语法：[HOST_IP:]HOST_PORT:CONTAINER_PORT[/PROTOCOL] */
export function checkShortSyntax(raw) {
  const value = unquote(raw);
  if (!value) return { ok: false, reason: "空的 port 条目" };

  const withoutProtocol = value.replace(/\/(?:tcp|udp)$/i, "");
  let hostPart = null;
  let rest = withoutProtocol;

  if (withoutProtocol.startsWith("[")) {
    const close = withoutProtocol.indexOf("]");
    if (close < 0) return { ok: false, reason: 'IPv6 字面量缺少 "]"：' + value };
    hostPart = withoutProtocol.slice(0, close + 1);
    rest = withoutProtocol.slice(close + 1);
    if (rest.startsWith(":")) rest = rest.slice(1);
  }

  const segments = splitHostPortSegments(rest);
  const lastTwo = segments.slice(-2).join(":");

  if (hostPart === null) {
    if (segments.length === 1) {
      return {
        ok: false,
        reason: "没有绑定地址：只写了容器端口，等价于绑 0.0.0.0",
        fix: '"127.0.0.1:' + value + '"',
      };
    }
    if (segments.length === 2) {
      return {
        ok: false,
        reason: "没有绑定地址：HOST:CONTAINER 两段式等价于绑 0.0.0.0，整个局域网都能访问",
        fix: '"127.0.0.1:' + value + '"',
      };
    }
    if (segments.length > 3) {
      return {
        ok: false,
        reason: "无法解析：冒号超过 3 段（" + value + "）。IPv6 必须写成 [::1]:2026:2026",
        fix: '"[::1]:' + lastTwo + '"',
      };
    }
    hostPart = segments[0];
  }

  const bind = checkBindAddress(hostPart);
  if (!bind.ok) {
    return { ok: false, reason: bind.reason, fix: '"127.0.0.1:' + lastTwo + '"' };
  }
  return { ok: true, value: bind.value };
}

/** 长语法：必须显式给 host_ip；缺 host_ip 等价于绑 0.0.0.0 */
export function checkLongSyntax(map) {
  for (const key of Object.keys(map)) {
    if (!LONG_SYNTAX_KEYS.has(key)) {
      return {
        ok: false,
        reason: '无法解析的长语法键 "' + key + '"（只认 ' + [...LONG_SYNTAX_KEYS].join(" / ") + "）",
      };
    }
  }
  const published = map.published;
  if (published === undefined || published === "") {
    // 只 declare 不 publish（等于 expose）：不是发布端口，不检查
    return { ok: true, skipped: true, value: null };
  }
  const hostIp = map.host_ip;
  if (hostIp === undefined || hostIp === "") {
    return {
      ok: false,
      reason: "长语法缺少 host_ip：published 的端口会被绑到 0.0.0.0",
      fix: 'host_ip: "127.0.0.1"（与 published: ' + published + " 并列）",
    };
  }
  const bind = checkBindAddress(hostIp);
  if (!bind.ok) {
    return { ok: false, reason: "host_ip " + bind.reason, fix: 'host_ip: "127.0.0.1"' };
  }
  return { ok: true, value: bind.value };
}

/* -------------------------------------------------------------------------- */
/* compose 解析（手写，只认缩进结构）                                            */
/* -------------------------------------------------------------------------- */

/**
 * 解析 compose 文本，抽出每个 service 的 ports 条目（带行号）。
 *
 * 返回 { services: [{ name, line, ports: [{ raw, line, kind, body?, map? }] }],
 *        problems: [{ line, raw, reason }] }
 * problems 一律算失败：宁可报"无法解析"，也不静默放过。
 */
export function parseComposePorts(text) {
  const services = [];
  const problems = [];
  const lines = text.split(/\r?\n/);

  let inServices = false;
  let servicesIndent = -1;
  let serviceIndent = -1;
  let current = null;

  let portsIndent = -1;
  let itemIndent = -1;
  let currentItem = null;

  const endPorts = () => {
    portsIndent = -1;
    itemIndent = -1;
    currentItem = null;
  };

  const pushProblem = (lineNo, raw, reason) => {
    problems.push({ line: lineNo, raw: raw, reason: reason });
  };

  lines.forEach((rawLine, index) => {
    const lineNo = index + 1;
    const code = stripComment(rawLine);
    if (!code.trim()) return;
    const indent = code.length - code.trimStart().length;
    const content = code.trim();

    /* ---------------- ports 块内部 ---------------- */
    if (portsIndent >= 0) {
      if (content.startsWith("- ") || content === "-") {
        if (indent <= portsIndent) {
          endPorts(); // 缩进退回去了：ports 块结束，交给下面的通用逻辑
        } else {
          const rest = content === "-" ? "" : content.slice(2).trim();
          currentItem = { raw: content, line: lineNo, body: rest, kind: "short", map: null };
          itemIndent = indent;
          current.ports.push(currentItem);

          if (!rest) {
            currentItem.kind = "long";
            currentItem.map = {};
          } else if (rest.startsWith("{")) {
            if (!rest.endsWith("}")) {
              pushProblem(lineNo, content, "无法解析：flow 映射缺少收尾的 }");
              currentItem.kind = "unknown";
            } else {
              const map = parseInlineMap(rest.slice(1, -1));
              if (!map) {
                pushProblem(lineNo, content, "无法解析：flow 映射里有不是 key: value 的项");
                currentItem.kind = "unknown";
              } else {
                currentItem.kind = "long";
                currentItem.map = map;
              }
            }
          } else {
            const pair = /^([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$/.exec(rest);
            if (pair && LONG_SYNTAX_KEYS.has(pair[1])) {
              currentItem.kind = "long";
              currentItem.map = {};
              currentItem.map[pair[1]] = unquote(pair[2]);
            } else if (pair) {
              pushProblem(
                lineNo,
                content,
                '无法解析：长语法不认识的键 "' + pair[1] + '"（也可能是短语法少写了引号）',
              );
              currentItem.kind = "unknown";
            }
          }
          return;
        }
      } else if (indent > itemIndent && currentItem) {
        const pair = /^([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$/.exec(content);
        if (!pair) {
          pushProblem(lineNo, content, "无法解析：ports 条目里出现了既不是列表项也不是 key: value 的行");
          return;
        }
        if (currentItem.kind !== "long" || !currentItem.map) {
          pushProblem(lineNo, content, "无法解析：短语法条目后面出现了额外的键值对");
          return;
        }
        currentItem.map[pair[1]] = unquote(pair[2]);
        return;
      } else {
        endPorts();
      }
    }

    /* ---------------- 通用结构 ---------------- */
    const keyMatch = /^([A-Za-z_][A-Za-z0-9_.-]*)\s*:(.*)$/.exec(content);
    if (!keyMatch) return;

    const key = keyMatch[1];
    const rest = keyMatch[2].trim();

    if (!inServices) {
      if (key === "services" && rest === "") {
        inServices = true;
        servicesIndent = indent;
        serviceIndent = -1;
      }
      return;
    }

    if (indent <= servicesIndent) {
      inServices = false;
      servicesIndent = -1;
      serviceIndent = -1;
      current = null;
      return;
    }

    if (serviceIndent < 0 || indent === serviceIndent) {
      if (rest !== "" && !rest.startsWith("{")) {
        pushProblem(lineNo, content, "无法解析：services 下的条目不是一个 service");
        return;
      }
      serviceIndent = indent;
      current = { name: key, line: lineNo, ports: [] };
      services.push(current);
      endPorts();
      return;
    }

    if (!current) return;

    if (key === "ports") {
      if (rest === "") {
        portsIndent = indent;
        itemIndent = -1;
        currentItem = null;
        return;
      }
      if (rest.startsWith("[")) {
        if (!rest.endsWith("]")) {
          pushProblem(lineNo, content, "无法解析：ports 的 flow 序列缺少收尾的 ]");
          return;
        }
        for (const item of splitFlowItems(rest.slice(1, -1))) {
          const entry = { raw: item, line: lineNo, body: item, kind: "short", map: null };
          if (item.startsWith("{")) {
            if (!item.endsWith("}")) {
              pushProblem(lineNo, item, "无法解析：flow 映射缺少收尾的 }");
              continue;
            }
            const map = parseInlineMap(item.slice(1, -1));
            if (!map) {
              pushProblem(lineNo, item, "无法解析：flow 映射里有不是 key: value 的项");
              continue;
            }
            entry.kind = "long";
            entry.map = map;
          } else {
            const pair = /^([A-Za-z_][A-Za-z0-9_]*)\s*:(.*)$/.exec(item);
            if (pair && LONG_SYNTAX_KEYS.has(pair[1])) {
              entry.kind = "long";
              entry.map = {};
              entry.map[pair[1]] = unquote(pair[2]);
            } else if (pair) {
              pushProblem(
                lineNo,
                item,
                '无法解析：长语法不认识的键 "' + pair[1] + '"（短语法要带引号）',
              );
              continue;
            }
          }
          current.ports.push(entry);
        }
        return;
      }
      pushProblem(
        lineNo,
        content,
        "无法解析：ports 的值既不是序列（- 开头）也不是 flow 序列（[ 开头）——不支持 YAML 锚点/别名",
      );
      return;
    }
  });

  return { services: services, problems: problems };
}

/* -------------------------------------------------------------------------- */
/* 检查                                                                        */
/* -------------------------------------------------------------------------- */

/** 违规信息里展示条目内容；长语法把它收集到的键值对也画出来，方便定位 */
function describeEntry(entry) {
  if (entry.kind === "long" && entry.map && Object.keys(entry.map).length) {
    const pairs = Object.keys(entry.map)
      .map((key) => key + ": " + entry.map[key])
      .join(", ");
    return entry.raw + "   { " + pairs + " }";
  }
  return entry.raw;
}

/** 把一个 ports 条目判定成违规 / 通过 */
export function evaluatePortEntry(entry) {
  if (entry.kind === "unknown") return { ok: false, reason: "无法解析的 ports 条目" };
  if (entry.kind === "long") return checkLongSyntax(entry.map || {});
  return checkShortSyntax(entry.body || entry.raw);
}

export function analyzeComposeText(text, file) {
  const name = file || "<text>";
  const parsed = parseComposePorts(text);
  const violations = [];
  const checked = [];

  for (const problem of parsed.problems) {
    violations.push({
      file: name,
      line: problem.line,
      raw: problem.raw,
      service: null,
      reason: problem.reason,
      fix: null,
    });
  }

  for (const service of parsed.services) {
    for (const entry of service.ports) {
      const result = evaluatePortEntry(entry);
      if (!result.ok) {
        violations.push({
          file: name,
          line: entry.line,
          raw: describeEntry(entry),
          service: service.name,
          reason: result.reason,
          fix: result.fix || null,
        });
        continue;
      }
      if (result.skipped) continue;
      checked.push({
        file: name,
        line: entry.line,
        raw: describeEntry(entry),
        service: service.name,
        bind: result.value,
      });
    }
  }

  return { file: name, services: parsed.services, violations: violations, checked: checked };
}

export function checkComposeFile(file) {
  return analyzeComposeText(fs.readFileSync(file, "utf8"), file);
}

/** 默认要扫的文件：仓库根 + frontend/ + frontend/docker/ 下的 compose 文件 */
export function defaultComposeFiles(root) {
  const base = root || REPO_ROOT;
  const files = [];
  const dirs = [base, path.join(base, "frontend"), path.join(base, "frontend", "docker")];
  for (const dir of dirs) {
    let names;
    try {
      names = fs.readdirSync(dir);
    } catch {
      continue;
    }
    for (const name of names) {
      if (!COMPOSE_FILE_RE.test(name)) continue;
      const full = path.join(dir, name);
      if (fs.statSync(full).isFile()) files.push(full);
    }
  }
  return [...new Set(files)].sort();
}

export function formatViolations(violations) {
  const lines = ["", "✗ published port 缺少显式绑定地址（M5 §7.3）", ""];
  for (const item of violations) {
    lines.push("  " + item.file + (item.line ? ":" + item.line : ""));
    lines.push("    条目: " + item.raw + (item.service ? "   （service: " + item.service + "）" : ""));
    lines.push("    问题: " + item.reason);
    if (item.fix) lines.push("    建议: " + item.fix);
    lines.push("");
  }
  lines.push("  背景：ports 的两种写法只差几个字符，肉眼 review 极易漏掉——");
  lines.push('        "2026:2026" 等价于 "0.0.0.0:2026:2026"，整个局域网都能访问。');
  lines.push("  容器内监听 0.0.0.0 是对的；只有 publish 出去的那个端口是攻击面。");
  lines.push("");
  return lines.join("\n");
}

export function runCheck(files) {
  const targets = files || defaultComposeFiles();
  if (targets.length === 0) {
    return {
      ok: false,
      files: targets,
      reports: [],
      violations: [
        { file: "<none>", line: 0, raw: "", service: null, reason: "没有找到任何 compose 文件", fix: null },
      ],
    };
  }
  const reports = targets.map((file) => checkComposeFile(file));
  const violations = reports.flatMap((report) => report.violations);
  return { ok: violations.length === 0, files: targets, reports: reports, violations: violations };
}

/** CLI 入口。返回进程退出码。 */
export function main(argv, log, logError) {
  const args = (argv || process.argv.slice(2)).filter((arg) => !arg.startsWith("-"));
  const out = log || console.log;
  const err = logError || console.error;

  const targets = args.length
    ? args.map((arg) => path.resolve(arg))
    : defaultComposeFiles();

  const files = targets.flatMap((target) => {
    try {
      return fs.statSync(target).isDirectory() ? defaultComposeFiles(target) : [target];
    } catch {
      return [target];
    }
  });

  const result = runCheck(files);
  const portCount = result.reports.reduce((sum, report) => sum + report.checked.length, 0);

  if (!result.ok) {
    err(formatViolations(result.violations));
    err("✗ " + result.violations.length + " 处违规（已检查 " + files.length + " 个 compose 文件、" + portCount + " 条合规的 published port）");
    return 1;
  }

  out("✓ " + files.length + " 个 compose 文件、" + portCount + " 条 published port 都带显式绑定地址");
  for (const report of result.reports) {
    const shown = path.relative(REPO_ROOT, report.file) || report.file;
    out("  · " + shown + "：" + report.checked.length + " 条 published port");
  }
  return 0;
}

const invokedDirectly = (() => {
  const entry = process.argv[1];
  if (!entry) return false;
  try {
    return fs.realpathSync(entry) === fs.realpathSync(fileURLToPath(import.meta.url));
  } catch {
    return path.resolve(entry) === fileURLToPath(import.meta.url);
  }
})();

if (invokedDirectly) {
  process.exitCode = main();
}
