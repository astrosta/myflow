# run_code 内置工具设计

> 状态：待确认（2026-11-05）
> 关系：新增 `Sandbox.run_code` 契约 + `run_code` 内置工具；OpenSandbox 走 Code Interpreter，
> 其余 provider 走通用命令通道。不改现有 bash / 文件工具语义。
> 相关：[OpenSandbox Provider 设计](2026-11-05-opensandbox-provider-design.md)

## 1. 目标

给模型一个"跑一段代码"的工具，而不是让它自己拼 `write_file + bash` 三步：

- 一次调用：传代码 → 拿到 stdout/stderr/结果；
- 有代码解释器能力的沙箱（OpenSandbox Code Interpreter）走真解释器：能返回表达式的值、
  支持多语言、可以带上下文；
- 没有解释器的沙箱（local / cube）用命令通道兜底，至少 python/node/bash 能用。

## 2. 契约

```python
class Sandbox:
    #: 该 provider 是否真的能跑代码（工具是否暴露由配置决定，见 §4）
    supports_run_code: bool = False

    def run_code(
        self,
        code: str,
        *,
        language: str = "python",
        timeout: float | None = None,
    ) -> str:
        raise SandboxRuntimeError("run_code is not supported by this provider")
```

- 返回**文本**（与 `execute_command` 一致）：stdout，非零退出追加 `exit code N`。
- 解释器返回的表达式结果以 `repr` 形式并进文本（Code Interpreter 的 `result` 项）。
- 语言集合：`python` / `javascript` / `bash` 走命令通道；`typescript` / `go` / `java` 只有
  Code Interpreter 支持，命令通道遇到它们要**明确报错**而不是猜。

## 3. 各 provider 的实现

| provider | run_code 实现 |
|---|---|
| `opensandbox` | 配了 `sandbox.opensandbox.run_code.image`（默认就是官方 code-interpreter 镜像）时，沙箱用该镜像 + entrypoint 创建，`run_code` 走 `CodeInterpreterSync.codes.run()`；否则退回命令通道 |
| `cube` | 命令通道：写 `<workspace>/.myflow-run/<uuid>.py` → `python3 file`（模板本来就要求 python3） |
| `local` | 命令通道，但**要求 `sandbox.allow_host_bash: true`**：这就是在宿主上跑模型代码，与 bash 工具同一个信任前提 |

命令通道是共享实现（`agentkit/sandbox/run_code.py`），只依赖 `Sandbox.write_file` /
`execute_command`，所以任何 provider 都能白拿；跑完删临时文件，不在 workspace 留垃圾。

## 4. 配置与暴露

```yaml
sandbox:
  run_code:
    enabled: false          # 默认关闭：不暴露 run_code 工具，模型看不见就不会调
    default_language: python
    timeout_seconds: 120    # 单次执行上限（与服务端/容器硬超时是两回事）

  opensandbox:
    run_code:
      image: sandbox-registry.cn-zhangjiakou.cr.aliyuncs.com/opensandbox/code-interpreter:v1.1.0
      entrypoint: ["/opt/code-interpreter/code-interpreter.sh"]
      ready_timeout_seconds: 30
```

- 工具**是否出现**只看 `sandbox.run_code.enabled`（与 bash 工具的 `allow_host_bash` 同款思路：
  模型看不见就不会调用）。不看 provider 类型，避免工具装配时去构造 provider。
- provider 真的跑不了时（local 未开 `allow_host_bash`、命令通道遇到 typescript），
  工具返回 `Error: ...`，与其它工具一致。
- `sandbox.run_code` 整段 hot-reload 安全（每次装配工具时读配置）。

## 5. 已验证的上游 API（opensandbox-code-interpreter）

```text
from code_interpreter import SupportedLanguage                     # BASH/GO/JAVA/JAVASCRIPT/PYTHON/TYPESCRIPT
from code_interpreter.sync.code_interpreter import CodeInterpreterSync

CodeInterpreterSync.create(sandbox: SandboxSync, *, ready_timeout=30s, skip_health_check=False)
: .codes  -> CodesSync
  .codes.run(code, *, language=None, context=None, handlers=None)
        -> opensandbox.models.execd.Execution(
              exit_code, result=[ExecutionResult], error, logs=ExecutionLogs(stdout/stderr))
  .codes.create_context(language) / delete_context(id)   # 有状态执行
```

注意：镜像需要专门的 code-interpreter 镜像与 entrypoint（不是普通 `python:3.12`），
集成测试要覆盖"CI 镜像下 bash/文件工具是否仍然可用"。

## 6. 测试

- 工具层：字符串返回、`Error:` 包装、路径脱敏、未启用时注册表里没有 `run_code`。
- 命令通道：语言→解释器映射、临时文件落在 workspace 且在 finally 删除、不支持的明确报错。
- opensandbox：`CodeInterpreterSync` 被调用（fake）、stdout+result 合并成文本、
  启用时 create 传的是 CI 镜像与 entrypoint。
- cube / local：命令通道复用；local 未开 `allow_host_bash` 时返回明确错误。
- 集成（opt-in）：真 server + CI 镜像跑 python 与 javascript，顺带确认 bash 仍可用。
