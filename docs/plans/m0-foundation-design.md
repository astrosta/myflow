# M0 地基详细设计

> 配套文档：[从零构建一个对标 DeerFlow 的 Agent 服务 —— 分阶段开发方案](greenfield-agent-service-roadmap.md)
>
> 本文细化 M0（1 周）。示例包名：框架层 `agentkit.*`，应用层 `app.*`。
> 所有"为什么"都对应 DeerFlow 现有实现里已经付过学费的地方，代码片段可直接改名使用。

---

## 1. M0 的范围

**做什么：** 让 `配置文件 → AppConfig → 模型实例` 这条链路跑通，并把后面 9 个阶段都要依赖的
四个基础设施定型：包边界、配置系统、路径系统、反射加载。

**不做什么（且要克制住）：**


| 不做                | 因为                           |
| ----------------- | ---------------------------- |
| FastAPI / 任何 HTTP | M3 才定 run 语义，现在写的路由 100% 会重写 |
| LangGraph 图       | M1                           |
| 数据库 / ORM         | M4。现在连不上表结构长什么样都不知道          |
| 前端                | M5                           |
| Docker            | M0 结束后再补，现在 Dockerfile 会天天变  |


M0 的唯一交付是**一个能被 import、能被测试、能加载配置、能造出模型对象的 Python 包**。

### 一周排期


| 天   | 内容                                                         |
| --- | ---------------------------------------------------------- |
| D1  | 目录骨架、双包 pyproject、uv workspace、ruff/pytest/pre-commit、边界测试 |
| D2  | 路径系统（`runtime_paths` + `Paths`）                            |
| D3  | 配置系统上半：`AppConfig` 骨架、YAML 加载、`$ENV` 解析、优先级解析              |
| D4  | 配置系统下半：内容签名缓存、热重载边界注册表、测试注入 API、版本检查                       |
| D5  | 反射系统 + 模型工厂 + 日志 + Makefile + 补测试                          |


---



## 2. 目录结构

```
myflow/
├── Makefile                          # 根编排（暂时只有 check/install/lint/test）
├── config.example.yaml               # 模板 → 复制为 config.yaml（gitignored）
├── extensions_config.example.json    # 模板 → 复制为 extensions_config.json（gitignored）
├── .env.example
├── pyproject.toml                    # uv workspace 根
├── backend/
│   ├── Makefile
│   ├── pyproject.toml                # 应用层依赖（依赖 agentkit）
│   ├── packages/
│   │   └── harness/
│   │       ├── pyproject.toml        # agentkit 包，可独立发布
│   │       └── agentkit/
│   │           ├── __init__.py
│   │           ├── config/
│   │           │   ├── __init__.py          # 只 re-export，不放实现
│   │           │   ├── app_config.py        # AppConfig + 单例层
│   │           │   ├── model_config.py
│   │           │   ├── sandbox_config.py    # 占位，M2 填
│   │           │   ├── file_signature.py
│   │           │   ├── reload_boundary.py
│   │           │   ├── runtime_paths.py
│   │           │   └── paths.py
│   │           ├── reflection/
│   │           │   ├── __init__.py
│   │           │   └── resolvers.py
│   │           ├── models/
│   │           │   ├── __init__.py
│   │           │   └── factory.py
│   │           └── utils/
│   │               ├── __init__.py
│   │               └── ids.py               # thread_id / user_id 校验
│   ├── app/
│   │   └── __init__.py                      # M0 只有空包，占位声明分层
│   └── tests/
│       ├── conftest.py
│       ├── test_harness_boundary.py
│       ├── test_harness_packaging.py
│       ├── test_app_config.py
│       ├── test_paths.py
│       ├── test_reflection.py
│       ├── test_model_factory.py
│       └── test_reload_boundary.py
└── docs/
```

**两个容易忽略的约定：**

`config/__init__.py` 只做 re-export，**不放任何实现**。原因是配置模块会被几乎所有模块 import，
一旦 `__init__` 里有逻辑或重量级 import，就会在 M7/M8 撞上循环引用，且很难拆。

`app/` 在 M0 就要建成空包。它什么都不做，存在的意义是让边界测试从第一天起就有两边可比。

---



## 3. 包与构建



### 3.1 双包 + uv workspace

根 `pyproject.toml`：

```toml
[tool.uv.workspace]
members = ["backend", "backend/packages/harness"]

[tool.uv.sources]
agentkit = { workspace = true }
```

`backend/packages/harness/pyproject.toml`（框架包）：

```toml
[project]
name = "agentkit"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.9",
    "pyyaml>=6.0",
    "python-dotenv>=1.0",
    "langchain-core>=0.3",
    "langchain>=0.3",
]

[project.optional-dependencies]
openai    = ["langchain-openai>=0.2"]
anthropic = ["langchain-anthropic>=0.3"]
deepseek  = ["langchain-deepseek>=0.1"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```



### 3.2 依赖分层原则：provider 一律走 extras

**核心依赖里不要出现任何具体的 LLM provider 包，也不要出现任何平台相关的包。**

DeerFlow 为此专门写了 `test_harness_packaging.py`，断言几个可选后端（BoxLite、OpenSandbox）
不在 `dependencies` 里而在 `optional-dependencies` 里。这个测试值得从 M0 就有：

```python
def test_providers_are_optional() -> None:
    pyproject = tomllib.loads(
        (HARNESS_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    core = pyproject["project"]["dependencies"]
    optional = pyproject["project"]["optional-dependencies"]

    for provider in ("langchain-openai", "langchain-anthropic", "langchain-deepseek"):
        assert not any(d.startswith(provider) for d in core), (
            f"{provider} must stay optional so core installs are provider-agnostic"
        )
    assert "openai" in optional
```

配套地，反射层要在 import 失败时给出**可执行的安装提示**（见 §6），否则用户只会看到
`ModuleNotFoundError: No module named 'langchain_google_genai'`，不知道该装哪个 pip 包
（模块名 `langchain_google_genai` ≠ 包名 `langchain-google-genai`）。

---



## 4. 分层边界测试（D1 就要有）

这是整个 M0 里最便宜、回报最高的一段代码。用 AST 扫描而不是正则，避免注释和字符串误报。

```python
"""Boundary check: harness layer must not import from app layer."""

import ast
from pathlib import Path

HARNESS_ROOT = Path(__file__).parent.parent / "packages" / "harness" / "agentkit"
BANNED_PREFIXES = ("app.",)


def _collect_imports(filepath: Path) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(filepath.read_text(encoding="utf-8"), filename=str(filepath))
    except SyntaxError:
        return []

    results: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            results.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            results.append((node.lineno, node.module))
    return results


def test_harness_does_not_import_app():
    violations = [
        f"  {py.relative_to(HARNESS_ROOT.parents[2])}:{lineno}  imports {module}"
        for py in sorted(HARNESS_ROOT.rglob("*.py"))
        for lineno, module in _collect_imports(py)
        if any(module == p.rstrip(".") or module.startswith(p) for p in BANNED_PREFIXES)
    ]
    assert not violations, "Harness must not import app:\n" + "\n".join(violations)
```

注意 `ast.walk` 会遍历到函数体内部的 import，所以"把违规 import 藏进函数里"这种绕过方式也会被抓到。
这是刻意的——延迟 import 可以用来打破循环依赖，但**不能**用来打破分层。

---



## 5. 配置系统（M0 的主体，约 2.5 天）



### 5.1 三份文件，职责不同


| 文件                       | 谁写              | 运行时可改   | 生产挂载  |
| ------------------------ | --------------- | ------- | ----- |
| `config.yaml`            | 运维              | 部分字段热生效 | `:ro` |
| `extensions_config.json` | **Gateway API** | 全部      | 读写    |
| `.env`                   | 运维              | 否       | —     |


M0 只需要把 `config.yaml` 做完整，`extensions_config.json` 留一个能读的空壳。但**两份文件的
分离必须在 M0 就成立**，因为它们的挂载方式、锁策略、原子写要求完全不同（详见路线图 M7 陷阱）。

第三方插件列表要放在 `config.yaml` 而不是 JSON 里。这不是随意的划分：插件列表会导致**代码被
import**，属于运维控制面，绝不能让一个 HTTP 接口能往里写东西。

### 5.2 配置文件解析优先级

`config.yaml` 和 `extensions_config.json` 用同一套四级优先级：

```
1. 显式 config_path 参数
2. MYFLOW_CONFIG_PATH 环境变量
3. 项目根目录下的 config.yaml
4. 兼容位置（backend/config.yaml、仓库根）
```

**一条关键的语义差别：** 第 1、2 级是"运维明确断言必须用这个文件"，文件不存在要抛
`FileNotFoundError`；第 3、4 级是搜索模式，找不到时对可选配置（如 extensions）返回 `None` 是合法的。
把这两种情况混成"找不到就用默认值"，会让一个打错的 `MYFLOW_CONFIG_PATH` 静默降级成默认配置，
线上排查起来极其痛苦。

### 5.3 AppConfig 骨架

```python
class AppConfig(BaseModel):
    config_version: int = Field(default=0)
    log_level: str = Field(default="info", description=format_field_description("log_level"))
    models: list[ModelConfig] = Field(default_factory=list)

    # M1+ 逐步填入，每个都是独立的子配置模块
    # sandbox / tools / skills / summarization / title / database / ...

    model_config = ConfigDict(extra="allow")

    _models_by_name: dict[str, ModelConfig] = PrivateAttr(default_factory=dict)
```

**子配置一律独立成模块。** `AppConfig` 只做组装，每个 section 一个文件（`model_config.py`、
`sandbox_config.py`……）。DeerFlow 的 `config/` 下现在有 47 个文件，`app_config.py` 本身
只剩下字段声明和加载逻辑。如果一开始就把所有 section 写在一个文件里，到 M6 那个文件会有 3000 行。

`extra="allow"` **的取舍。** 允许未知字段的好处是接新 provider 时不用改 schema（provider 特有的
`api_version`、`organization` 之类直接透传）。代价是**配置项打错字不会报错**——`maxx_tokens`
会被原样传给构造函数，然后在请求时被 SDK 拒绝，错误信息完全看不出是配置笔误。
补偿措施放在模型工厂里（§7.3）。

### 5.4 `$ENV` 变量解析

递归遍历整棵配置树，把以 `$` 开头的字符串替换成环境变量。**找不到时必须抛异常而不是返回空串**：

```python
@classmethod
def resolve_env_variables(cls, config: Any) -> Any:
    if isinstance(config, str):
        if config.startswith("$"):
            value = os.getenv(config[1:])
            if value is None:
                raise ValueError(f"Environment variable {config[1:]} not found for config value {config}")
            return value
        return config
    if isinstance(config, dict):
        return {k: cls.resolve_env_variables(v) for k, v in config.items()}
    if isinstance(config, list):
        return [cls.resolve_env_variables(item) for item in config]
    return config
```

静默返回空串的后果是 API key 变成 `""`，然后你会收到一个 401 并花半小时怀疑是供应商问题。

### 5.5 缓存失效用内容签名，不要用 mtime

这是 M0 里最容易做错、且后果最隐蔽的一处。

`get_app_config()` 要缓存解析结果，但配置文件被改了要能自动重载。**用 mtime 判断是不够的**，
以下场景 mtime 全部会骗你：

- 同一秒内的两次编辑
- `git checkout` / `cp -p` / `tar` / `rsync` 保留或回退时间戳
- 对象存储、NFS 等网络挂载上 mtime 不更新
- 切换到另一个 mtime 更小的文件

正确做法是 `(mtime, size, sha256)` 三元组：

```python
ConfigSignature = tuple[float | None, int | None, str | None]

def get_config_signature(config_path: Path) -> ConfigSignature | None:
    try:
        st = config_path.stat()
    except OSError:
        return None   # 文件不存在，和"存在但读不了"是不同的情况

    # 永远算完整 hash，不要在 mtime/size 命中时短路：
    # 同一秒内换成等长的不同内容，只有 sha256 能发现。
    digest = hashlib.sha256()
    try:
        with config_path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return (st.st_mtime, st.st_size, None)
    return (st.st_mtime, st.st_size, digest.hexdigest())
```

配置文件通常只有几十 KB，每次请求算一次 sha256 的成本可以忽略；真正的成本是缓存不失效导致的
"改了配置没生效"，这个问题在多人协作时会反复消耗排查时间。

### 5.6 热重载边界注册表

**这是 M0 里最值得提前做的一个"看起来没必要"的东西。**

配置字段分两类：per-run 字段（下一条消息就生效）和基础设施字段（必须重启进程）。区别的来源是
后者在启动时被捕获进了某个单例——数据库引擎、checkpointer、日志 handler、IM 客户端。

如果不做集中登记，会发生两件事：一是运维改了 `database.backend` 发现没生效但没有任何提示；
二是几个月后没人说得清哪些字段能热改。

```python
STARTUP_ONLY_PREFIX = "startup-only:"

STARTUP_ONLY_FIELDS: dict[str, str] = {
    "log_level": (
        "apply_logging_level() 只在启动时运行；重载后的 AppConfig 不会重新触发它。"
    ),
    # M4 加 database / checkpointer；M9 加 scheduler / channels ...
}


def format_field_description(field_path: str, *, field_doc: str | None = None) -> str:
    reason = STARTUP_ONLY_FIELDS[field_path]     # 未注册直接 KeyError，不给占位符
    header = f"{STARTUP_ONLY_PREFIX} {reason}"
    return header if field_doc is None else f"{header}\n\n{field_doc.strip()}"
```

配套一个**双向漂移测试**：注册表里的字段，其 `Field(description=...)` 必须带
`startup-only:` 前缀；反过来，任何带这个前缀的字段必须在注册表里。

```python
def test_startup_only_registry_matches_schema():
    for path in STARTUP_ONLY_FIELDS:
        if "." in path:
            continue  # 嵌套字段单独断言
        field = AppConfig.model_fields.get(path)
        if field is None:
            continue  # 非 schema 字段（如 channels）只存在于注册表
        assert field.description.startswith(STARTUP_ONLY_PREFIX)

    for name, field in AppConfig.model_fields.items():
        if (field.description or "").startswith(STARTUP_ONLY_PREFIX):
            assert name in STARTUP_ONLY_FIELDS
```

好处是这条约束会出现在 IDE 的悬停提示里——改字段的人不用翻文档就知道要不要重启。

### 5.7 三个 YAML 加载的边角情况

**注释掉整段会解析成** `None`**。** 用户照文档执行 `cp config.example.yaml config.yaml`，而模板里
`models:` 下面全是注释，PyYAML 会把值解析成 `None`，pydantic 报
`Input should be a valid list`。第一次运行就崩，且错误信息完全看不出原因。

```python
@model_validator(mode="before")
@classmethod
def _drop_null_config_sections(cls, data: Any) -> Any:
    """present-but-null 的 section 当作缺失，让默认值生效。"""
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if v is not None}
    return data
```

注意这个策略对**没有默认值的必填 section**仍然会报错，这是对的——没有东西可回退。

**配置版本检查。** `config.example.yaml` 里放一个 `config_version`，加载时对比用户的版本，
低了就 warning 提示运行 `make config-upgrade`。改 schema 时记得 bump。缺失字段视为版本 0。

**零模型配置要 warning 而不是报错。** 新用户第一次跑起来时 `models` 是空的，直接崩会让人
以为装坏了。给一条指向 `config.example.yaml` 的明确提示更好。

### 5.8 单例层与测试注入

`get_app_config()` 是个全局单例，这在测试里是个麻烦。要同时提供三种覆盖方式：

```python
def get_app_config() -> AppConfig: ...          # 带签名失效的缓存读取
def reload_app_config(path=None) -> AppConfig:  # 强制重载
def reset_app_config() -> None:                 # 清缓存（测试 teardown）
def set_app_config(config: AppConfig) -> None:  # 注入 mock（全局）

def push_current_app_config(config: AppConfig) -> None:  # ContextVar 作用域覆盖
def pop_current_app_config() -> None:
def peek_current_app_config() -> AppConfig | None:
```

前四个是进程级的，后三个是 ContextVar 的。**ContextVar 那一组不是多余的**：M9 做多租户时
需要在一次请求的作用域内覆盖配置，而不能污染其他并发请求。push/pop 要维护一个栈，
支持嵌套覆盖。

`conftest.py` 里加一个 autouse fixture 在每个测试后 `reset_app_config()`，否则测试之间会串配置，
且失败顺序依赖，极难定位。

### 5.9 名称索引

`get_model_config(name)` 在 agent 构建时会被调用好几次，`get_tool_config(name)` 在每次社区工具
调用时会被调用 2–3 次。用 `next(m for m in self.models if ...)` 的 O(n) 扫描在热路径上。
在 `model_validator(mode="after")` 里建一次字典：

```python
@model_validator(mode="after")
def _build_name_indexes(self) -> "AppConfig":
    by_name: dict[str, ModelConfig] = {}
    for m in self.models:
        by_name.setdefault(m.name, m)   # setdefault 保留首个，和原 next() 首匹配语义一致
    self._models_by_name = by_name
    return self
```

用 `PrivateAttr` 存，不会被序列化；配置重载会构造新的 `AppConfig`，索引自然刷新。

---



## 6. 路径系统（D2，约 0.5 天）



### 6.1 两层：runtime_paths 决定根，Paths 决定布局

`runtime_paths.py` 回答"文件放在哪个根下"：

```python
def project_root() -> Path:
    """运行时文件的项目根。"""
    if env_root := os.getenv("MYFLOW_PROJECT_ROOT"):
        root = Path(env_root).resolve()
        if not root.exists():
            raise ValueError(f"MYFLOW_PROJECT_ROOT='{env_root}' resolved to '{root}' which does not exist.")
        if not root.is_dir():
            raise ValueError(f"MYFLOW_PROJECT_ROOT='{env_root}' resolved to '{root}' which is not a directory.")
        return root
    return Path.cwd().resolve()


def runtime_home() -> Path:
    """可写的状态目录。"""
    if env_home := os.getenv("MYFLOW_HOME"):
        return Path(env_home).resolve()
    return project_root() / ".myflow"


def resolve_path(value: str | os.PathLike[str], *, base: Path | None = None) -> Path:
    """绝对路径原样返回，相对路径按项目根解析。"""
    path = Path(value)
    if not path.is_absolute():
        path = (base or project_root()) / path
    return path.resolve()
```

环境变量设错时**要立刻抛异常**，不要静默回退到 `cwd`。否则数据会悄悄写到另一个目录，
等发现时已经分散在两处。

`paths.py` 回答"目录长什么样"：

```
{runtime_home}/
├── memory.json
├── USER.md                        # 全局用户画像（M10）
├── agents/{agent_name}/           # 自定义 agent（M9）
└── threads/{thread_id}/
    └── user-data/                 # 沙箱里挂成 /mnt/user-data/
        ├── workspace/
        ├── uploads/
        └── outputs/
```

M0 只需要建出 `threads/{thread_id}/user-data/{workspace,uploads,outputs}` 这一支，
并定义 `VIRTUAL_PATH_PREFIX = "/mnt/user-data"`。

### 6.2 ID 校验现在就写

任何会进入文件系统路径的 ID 都要先过白名单校验，且校验函数放在 `Paths` 之外单独成模块，
因为 M3 的路由层也要用。

```python
_SAFE_USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

def validate_user_id(user_id: str) -> str:
    if not _SAFE_USER_ID_RE.match(user_id):
        raise ValueError(f"Invalid user_id {user_id!r}: only alphanumerics, hyphens and underscores are allowed.")
    return user_id
```

顺带写好外部身份的规范化函数。M9 接 IM 时，飞书/Slack 的用户 ID 含有不合法字符，直接
replace 会让两个不同 ID 落到同一个存储桶：

```python
def make_safe_user_id(raw: str) -> str:
    sanitized = _UNSAFE_CHAR_RE.sub("-", raw)
    if sanitized == raw:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{sanitized}-{digest}"   # 摘要后缀保证不碰撞
```



### 6.3 Windows 宿主路径要保留原生风格

如果将来要在 Windows 上跑 Docker，bind mount 的源路径必须保持 `C:\repo\...` 形式。
在 POSIX 上用 `Path(base) / part` 拼接 Windows 路径会产生混合分隔符，Docker Desktop 会拒绝。

```python
def join_host_path(base: str, *parts: str) -> str:
    if re.match(r"^[A-Za-z]:[\\/]", base) or base.startswith("\\\\") or "\\" in base:
        result = PureWindowsPath(base)
    else:
        result = Path(base)
    for part in parts:
        result /= part
    return str(result)
```

这个函数只用于**宿主侧**路径拼接，容器内路径一律 POSIX。

---



## 7. 反射系统与模型工厂（D5）



### 7.1 反射层

两个函数，`resolve_class` 建立在 `resolve_variable` 之上。路径格式用冒号分隔
（`module.path:Name`）而不是点号，因为点号无法区分"子模块"和"模块内的属性"。

```python
def resolve_variable[T](variable_path: str, expected_type: type[T] | tuple[type, ...] | None = None) -> T:
    try:
        module_path, variable_name = variable_path.rsplit(":", 1)
    except ValueError as err:
        raise ImportError(f"{variable_path} doesn't look like a variable path. Example: package.module:name") from err

    try:
        module = import_module(module_path)
    except ImportError as err:
        module_root = module_path.split(".", 1)[0]
        if isinstance(err, ModuleNotFoundError) or getattr(err, "name", None) == module_root:
            raise ImportError(f"Could not import module {module_path}. {_missing_dependency_hint(module_path, err)}") from err
        raise ImportError(f"Error importing module {module_path}: {err}") from err   # 保留原始信息

    try:
        variable = getattr(module, variable_name)
    except AttributeError as err:
        raise ImportError(f"Module {module_path} does not define {variable_name}") from err

    if expected_type is not None and not isinstance(variable, expected_type):
        raise ValueError(f"{variable_path} is not an instance of the expected type, got {type(variable).__name__}")
    return variable
```

**区分"模块本身缺失"和"模块内部 import 失败"。** 前者给安装提示，后者要原样保留异常信息。
把两者混成一句"请安装 X"，会让一个 provider 包内部的真实 bug 被包装成误导性的安装提示。

安装提示要做模块名 → pip 包名的映射，因为两者经常不同：

```python
MODULE_TO_PACKAGE_HINTS = {
    "langchain_google_genai": "langchain-google-genai",
    "langchain_anthropic": "langchain-anthropic",
    "langchain_openai": "langchain-openai",
}
# 未登记的走 module_root.replace("_", "-") 兜底
```

还有一个细节：`langchain_google_genai` 的 import 失败可能是由传递依赖 `google` 触发的，
这时 `err.name` 是 `google` 而不是 `langchain_google_genai`。**优先用模块根名去查映射表**，
提示装 `langchain-google-genai` 才是对的。

### 7.2 ModelConfig 字段设计

必须在 M0 就存在的字段（后面阶段会依赖）：


| 字段                                                                       | 谁在用                          |
| ------------------------------------------------------------------------ | ---------------------------- |
| `name` / `display_name` / `description`                                  | M3 的 `/api/models`、M5 的模型选择器 |
| `use`                                                                    | 反射类路径                        |
| `model`                                                                  | provider 侧模型 ID              |
| `supports_thinking` + `when_thinking_enabled` / `when_thinking_disabled` | M5 的思考开关                     |
| `supports_vision`                                                        | M6 的 `ViewImageMiddleware`   |
| `context_window`                                                         | M5 的上下文占用百分比、M6 的按比例压缩触发     |
| `max_tokens`                                                             | 每次调用的输出上限                    |


`context_window` 和 `max_tokens` 是**两个不同的东西**（总窗口 vs 单次输出上限），
命名上要在 description 里写清楚，否则 100% 会被配错。

### 7.3 `create_chat_model` 的处理流水线

顺序不能乱：

```
1. 取 ModelConfig
2. model_dump(exclude_none=True, exclude={元数据字段})     ← 排除不能进构造函数的字段
3. 叠加 caller 的 model_overrides（忽略 None）
4. thinking 开 / 关的分支处理
5. api_base → base_url 别名归一                            ← 必须在 6、7 之前
6. stream_chunk_timeout 默认值注入
7. provider 特化（Codex 去掉 max_tokens 等）
8. stream_usage 默认 True
9. 未知 key 告警
10. 构造实例
11. 构造后注入 profile（context_window → max_input_tokens）
```

**第 2 步的 exclude 集合要完整。** `display_name`、`description`、`supports_`*、
`when_thinking_*`、`context_window`、`pricing` 全是元数据，传给 provider 客户端会被塞进
`model_kwargs` 然后在请求时炸掉。漏掉任何一个都是个难查的 bug。

**第 5 步必须在 6、7 之前。** 下游的 OpenAI 兼容性判断要看到规范化后的 endpoint key。

**第 11 步必须在构造之后。** 把 `profile` 传给构造函数会**整体替换**掉 provider 推断出来的
元数据（tool_calling、structured_output、输出上限），只留下你设的那一个 key。正确做法是
构造完再 merge：

```python
inferred = getattr(model_instance, "profile", None)
model_instance.profile = {**(inferred or {}), "max_input_tokens": model_config.context_window}
```



### 7.4 三个必踩的 provider 坑（提前埋防护）

**坑一：**`api_base` **vs** `base_url`**。** `BaseChatOpenAI` 系列接受的是 `base_url`，但很多文档和
配置示例写的是 `api_base`。因为 `extra="allow"`，这个 key 会被透传给构造函数，构造函数不报错
但会把它转移进 `model_kwargs`，然后展开进每次 `Completions.create()`，被 OpenAI SDK 以
`unexpected keyword argument 'api_base'` 拒绝——**而且 endpoint 覆盖被静默丢弃了**。

防护：对 `issubclass(model_class, BaseChatOpenAI)` 且自身未声明 `api_base` 字段的类，
自动把 `api_base` 改名成 `base_url`。注意要按基类判断而不是维护类路径白名单，
否则每加一个 provider 都要记得改这张表。

**坑二：配置项打错字静默失败。** 同样因为 `extra="allow"`，`maxx_tokens` 不会在配置加载时报错。
在模型构建时对 OpenAI 兼容族做一次已知字段（含 alias）比对，未知 key 打 warning：

```python
logger.warning(
    "Model '%s' (%s): config key(s) %s are not recognized and will be forwarded as-is; "
    "this may raise at request time. Check for typos (e.g. 'maxx_tokens' -> 'max_tokens').",
    model_name, model_class.__name__, unknown,
)
```

这个检查**只对 OpenAI 兼容族做**。其他 provider（如 `ChatAnthropic`）对额外 kwargs 的处理
方式不同，套用同一张白名单会误报。

**坑三：第三方 endpoint 丢失 token 统计。** LangChain 的 `BaseChatOpenAI` 只在没有自定义
`base_url` 时才默认 `stream_usage=True`。接 DeepSeek、豆包这类 OpenAI 兼容网关时，
流式响应里就没有 usage 元数据了，M6 的 token 统计会全是 0。显式默认打开：

```python
if "stream_usage" not in settings and "stream_usage" in getattr(model_class, "model_fields", {}):
    settings["stream_usage"] = True
```

---



## 8. 日志

M0 只做两件事：

```python
def apply_logging_level(name: str | None) -> None:
    """只改本项目的 logger 层级，不动第三方库。"""
    level = logging.getLevelNamesMapping().get((name or "info").strip().upper(), logging.INFO)
    for logger_name in ("agentkit", "app"):
        logging.getLogger(logger_name).setLevel(level)
    for handler in logging.root.handlers:
        if level < handler.level:
            handler.setLevel(level)   # 只降不升，保留对第三方日志的刻意抑制
```

**只调本项目的两个 logger 层级**，不要改 root。把 root 设成 DEBUG 会让 uvicorn、sqlalchemy、
httpx 的日志淹没有用信息。root handler 的阈值只降不升，这样配置的消息能透出来，同时保留
对第三方输出的抑制。

请求级 trace id（`X-Trace-Id` + ContextVar）留到 M3，因为 M0 还没有"请求"这个概念。
但要预留 `logging.enhance` 配置块，并从一开始就注册为 startup-only。

---



## 9. 工具链与 Makefile

```makefile
# 根 Makefile
check:            ## 检查 python/uv/node 版本
config:           ## config.example.yaml -> config.yaml（已存在则跳过）
install:          ## uv sync + pre-commit install
lint:             ## ruff check
format:           ## ruff format
test:             ## pytest（默认离线套件）
```

ruff 配置：line-length 与团队约定一致（DeerFlow 用 240），双引号，空格缩进，Python 3.12+ 全量类型注解。
pre-commit 挂 `ruff check --fix` + `ruff format`。CI 用 `ruff format --check` 硬卡。

**测试分层从 M0 就建立 marker**，哪怕现在只有一层：

```toml
[tool.pytest.ini_options]
markers = [
    "live: hits real external APIs; excluded from the default suite",
    "blocking_io: strict sync-IO-in-async-context gate",
]
addopts = "-m 'not live and not blocking_io'"
```

`make test` 跑默认套件，`make test-live` 显式开 live。不这么分，几个月后 CI 会因为某个人的
API 配额跑完而红，然后大家就开始习惯性忽略 CI。

---



## 10. M0 的测试清单


| 测试文件                        | 覆盖                                                                                                                   |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `test_harness_boundary.py`  | harness 不 import app                                                                                                 |
| `test_harness_packaging.py` | provider 包在 optional-dependencies 里                                                                                  |
| `test_app_config.py`        | 四级路径优先级；显式路径缺失抛异常 vs 搜索模式返回 None；`$ENV` 解析成功/缺失抛异常；null section 降级为默认；版本过低 warning；名称索引首匹配语义；`set/reset/push/pop` 语义 |
| `test_config_signature.py`  | 同秒等长改内容能被检出；mtime 回退能被检出；文件不存在返回 None；stat 成功但读失败返回部分签名                                                              |
| `test_reload_boundary.py`   | 注册表 ↔ schema description 双向一致                                                                                        |
| `test_paths.py`             | 虚拟路径映射；非法 user_id / thread_id 被拒；`make_safe_user_id` 不碰撞；Windows 路径拼接保留风格；`MYFLOW_PROJECT_ROOT` 指向不存在路径时抛异常          |
| `test_reflection.py`        | 正常解析；路径格式错误；模块缺失给出安装提示；模块内部 ImportError 保留原信息；`expected_type` 校验；`base_class` 校验                                     |
| `test_model_factory.py`     | 元数据字段不进构造函数；`api_base` 被改名；未知 key 告警；`stream_usage` 默认；thinking 开关分支；`context_window` 在构造后 merge 进 profile 而非替换      |


模型工厂的测试用一个 fake `BaseChatModel` 子类，不要真连 provider。

---



## 11. 验收清单

- [ ] `uv sync` 从零安装成功，且**不含任何 LLM provider 包**
- [ ] `uv sync --extra openai` 后 `create_chat_model("gpt-4o")` 能造出实例
- [ ] 未装 provider 时的报错包含 `uv add langchain-xxx` 的可执行提示
- [ ] 改 `config.yaml` 后**不重启**，下一次 `get_app_config()` 拿到新值
- [ ] 同一秒内把配置改成等长的不同内容，同样能被检出
- [ ] `cp config.example.yaml config.yaml` 后直接启动不崩（全注释的 section 走默认值）
- [ ] `MYFLOW_CONFIG_PATH` 指向不存在的文件 → 明确报错，不静默降级
- [ ] 在 harness 里写一句 `from app.x import y` → CI 红
- [ ] 给一个 startup-only 字段漏掉 description 前缀 → CI 红
- [ ] `make lint && make format && make test` 全绿

---



## 12. 刻意不做的事，以及为什么


| 不做                             | 理由                                            |
| ------------------------------ | --------------------------------------------- |
| 配置文件的跨进程写锁                     | M0 还没有会写配置的 API。M7 引入 Gateway 写入时再加，那时才知道要锁什么 |
| 配置的 JSON Schema 导出             | 等 schema 稳定（约 M6）再做，否则天天重新生成                  |
| `extensions_config.json` 的完整实现 | M7。M0 只留一个能读的空壳和路径解析                          |
| trace id / 结构化日志               | M3。M0 没有请求边界                                  |
| Docker / Helm                  | M0 结束后，配置形状稳定了再写                              |
| 配置热重载的单例重置联动                   | 等 M4 有了数据库单例再做。现在没有单例需要重置                     |


---



## 13. 一句话总结

M0 的产出看起来只是"能读个 YAML 造个模型"，但它定下的是**后面 9 个阶段所有模块共享的
四个坐标系**：模块之间怎么 import（分层）、配置从哪来怎么失效（配置系统）、文件放哪儿
（路径系统）、外部实现怎么接进来（反射）。

这四个里任何一个在 M5 之后才想改，影响面都是全仓库级的。