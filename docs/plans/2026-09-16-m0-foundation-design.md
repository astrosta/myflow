# M0 地基详细设计

> 配套文档：[从零构建一个对标 DeerFlow 的 Agent 服务 —— 分阶段开发方案](2026-09-16-greenfield-agent-service-roadmap.md)
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
import**，属于运维控制面，绝不能让一个 HTTP 接口能往里写东西。模型的 `use` 字段同理——
它也是代码执行入口，所以 `config.yaml` 整份都不能变成 API 可写。

`.env` **的加载时机是个顺序坑。** `config.yaml` 里写的 `$OPENAI_API_KEY` 要靠 §5.4 的解析器去
读环境变量，而这些变量通常来自 `.env`。如果 dotenv 比配置解析晚加载，所有 `$ENV` 引用都会抛
"环境变量不存在"。

可靠的做法是**把** `load_dotenv()` **放在配置模块的模块级**，而不是某个 `main()` 或 `lifespan` 里：

```python
# agentkit/config/app_config.py
from dotenv import load_dotenv

load_dotenv()          # 模块导入即生效，早于任何 AppConfig.from_file()
```

理由是配置模块必然先于一切配置读取被 import，把 dotenv 挂在这里，就不存在"某个入口忘了初始化"
的可能。放在 `main()` 里的话，测试、CLI、脚本这些不走 `main()` 的入口会各自缺失一次。

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

#### `from_file()` 的加载流水线

七步，**顺序不能调**：

```python
@classmethod
def from_file(cls, config_path: str | None = None) -> Self:
    resolved_path = cls.resolve_config_path(config_path)          # 1. 四级优先级（§5.2）

    with open(resolved_path, encoding="utf-8") as f:
        config_data = yaml.safe_load(f) or {}                     # 2. 空文件 -> {}，不是 None

    cls._check_config_version(config_data, resolved_path)         # 3. 在原始数据上查版本
    config_data = cls.resolve_env_variables(config_data)          # 4. $ENV 展开（§5.4）
    cls._apply_section_defaults(config_data)                      # 5. 复杂默认值

    result = cls.model_validate(config_data)                      # 6. pydantic 校验 + 建索引

    if not result.models:                                         # 7. 校验后的可用性提示
        logger.warning("No models configured in %s. See config.example.yaml.", resolved_path)
    return result
```

**第 2 步的** `or {}` **不能省。** 空文件和只有注释的文件，`yaml.safe_load` 都返回 `None`，
直接喂给 pydantic 会得到一条 "Input should be a valid dictionary" ——用户刚 `cp` 完模板就撞上，
而错误信息完全指不出是"文件是空的"。这和 §5.7 的 null section 是同一类问题的两个层级：
**整个文件是 null** 在这里处理，**某个 section 是 null** 在验证器里处理。

**第 3 步必须在第 4 步之前。** 版本过旧的配置很可能引用了已经改名的环境变量，先跑 `$ENV` 解析
会抛出 "环境变量 XXX 不存在"，把用户引向错误的方向。先报"你的配置是版本 0，跑
`make config-upgrade`"，那才是可执行的下一步。

**第 4 步必须在第 6 步之前。** 这条更硬：`port: $DB_PORT` 这种字段的声明类型是 `int`，
如果先校验，pydantic 看到的是字符串 `"$DB_PORT"`，直接类型错误。必须先把 `$ENV` 换成真实值，
pydantic 才能做类型强转。

**第 7 步只能 warning，不能 raise。** 新用户第一次跑起来时 `models` 必然是空的，直接崩会让人
以为装坏了。这是"配置不完整"而不是"配置错误"——前者给指引，后者才抛异常。

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
    # M4 加 database / checkpointer；M8 加 subagent_runtime.max_running 等；
    # M9 加 runs.lease_seconds / stream_bridge.backend / scheduler.max_concurrent_runs 等；
    # M10 加 plugins（整段，import 即执行）
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

#### `get_app_config()` 的四级解析顺序

```python
_app_config: AppConfig | None = None
_app_config_path: Path | None = None
_app_config_mtime: float | None = None
_app_config_signature: ConfigSignature | None = None
_app_config_is_custom = False                       # set_app_config() 注入过

_current_app_config: ContextVar[AppConfig | None] = ContextVar("current_app_config", default=None)


def get_app_config() -> AppConfig:
    # 1. 作用域覆盖优先，且完全不碰磁盘
    runtime_override = _current_app_config.get()
    if runtime_override is not None:
        return runtime_override

    # 2. 注入的 mock 永不被磁盘重载顶掉
    if _app_config is not None and _app_config_is_custom:
        return _app_config

    # 3. 路径或内容签名变了就重载
    resolved_path = AppConfig.resolve_config_path()
    current_signature = get_config_signature(resolved_path)
    should_reload = _app_config is None or _app_config_path != resolved_path or _app_config_signature != current_signature
    if should_reload:
        _load_and_cache_app_config(str(resolved_path))

    # 4. 返回缓存
    return _app_config
```

**第 2 步的** `_is_custom` **标志是最容易漏、也最难查的一处。** 没有它，测试里
`set_app_config(mock)` 之后只要有任何一次签名比对发现磁盘文件变了（比如另一个测试恰好写了
临时配置），mock 就被静默换成磁盘上的真配置。表现是"单独跑过、一起跑挂"，而且挂的地方
离 `set_app_config` 很远。**注入是一个断言，不是一个提议**——注入之后就不该再看磁盘。

**同时维护** `mtime` **和** `signature` **两个缓存键，但只有 signature 参与重载判定。** mtime 的唯一
用途是让日志能区分两种情况：

```python
if _app_config_mtime != current_mtime:
    logger.info("Config file modified (mtime %s -> %s), reloading", _app_config_mtime, current_mtime)
elif _app_config_signature != current_signature:
    logger.info("Config content signature changed (mtime unchanged), reloading")
```

第二条日志正是 §5.5 那些 mtime 骗人的场景，单独打出来，排查时能一眼确认签名机制真的在工作。

**路径变化也要触发重载**（`_app_config_path != resolved_path`）。运行时改 `MYFLOW_CONFIG_PATH`
不常见，但漏了这条会出现"换了配置文件却还在用旧的"，且签名比对不会发现——因为它比的是
新路径的签名和旧路径的签名，两个不同文件的签名不同，恰好也会重载。这条严格说是冗余的，
但写出来让意图明确：**缓存键是 (路径, 内容)，不只是内容。**

#### push/pop 的栈必须是不可变元组

```python
_current_app_config_stack: ContextVar[tuple[AppConfig | None, ...]] = ContextVar("stack", default=())


def push_current_app_config(config: AppConfig) -> None:
    stack = _current_app_config_stack.get()
    _current_app_config_stack.set(stack + (_current_app_config.get(),))   # 元组拼接，产生新对象
    _current_app_config.set(config)


def pop_current_app_config() -> None:
    stack = _current_app_config_stack.get()
    if not stack:
        _current_app_config.set(None)
        return
    _current_app_config_stack.set(stack[:-1])
    _current_app_config.set(stack[-1])
```

**用** `list` **并** `append`**/**`pop` **会破坏 ContextVar 的隔离语义。** ContextVar 隔离的是"名字绑到哪个
对象"，不是对象本身。多个 context 拿到的是同一个 list 实例，一个 context 里 `append` 另一个
立刻可见——多租户的并发请求会互相污染，而且是概率性的。元组拼接每次产生新对象，`set()` 才
真正完成了绑定切换。

`pop` 在空栈上要**降级成清空而不是抛异常**。异常路径下的 pop 可能比 push 多一次（比如 push 之前
就抛了），让清理逻辑能无条件 pop，比要求每个调用点自己配平更可靠。

#### conftest.py：配置隔离是 autouse 的

```python
# tests/conftest.py
@pytest.fixture(autouse=True)
def _isolate_app_config():
    yield
    reset_app_config()          # 清进程单例 + _is_custom 标志
    pop_current_app_config()    # 清可能泄漏的作用域覆盖（空栈安全）


@pytest.fixture
def app_config(tmp_path) -> AppConfig:
    """写一份临时 config.yaml 并加载，不碰仓库里的真文件。"""
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_CONFIG_YAML, encoding="utf-8")
    return AppConfig.from_file(str(path))
```

**必须是** `autouse`**。** 靠每个测试自觉调用 teardown，总会有人忘，而后果是顺序依赖的串配置——
这类失败在本地单跑永远复现不了，只在 CI 的某个分片顺序下出现。

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

### 5.10 `config.example.yaml` 的最小形状

模板本身是 M0 的交付物之一，而且是用户看到的第一样东西。M0 阶段它应该长这样：

```yaml
# ============================================================
# MyFlow 配置模板 —— 复制为 config.yaml 后编辑（config.yaml 已 gitignored）
#   cp config.example.yaml config.yaml
# ============================================================

# schema 版本。改 schema 时 bump；加载时比对，过低会提示升级。
config_version: 1

# 只影响 myflow / app 两个 logger，不动第三方库。
# startup-only: 改了要重启（apply_logging_level 只在启动时跑一次）
log_level: info

# ------------------------------------------------------------
# 模型。列表顺序有意义：不指定模型时用第一个。
# 以 $ 开头的值从环境变量读取；读不到会直接报错，不会静默变成空串。
# ------------------------------------------------------------
models:
  - name: gpt-4o                       # 唯一标识，API 和前端用这个名字选模型
    display_name: GPT-4o               # 给人看的，不会传给 provider
    description: OpenAI 旗舰多模态模型
    use: langchain_openai:ChatOpenAI   # 反射类路径。这是代码执行入口，故本文件不可 API 写
    model: gpt-4o                      # provider 侧的模型 ID
    api_key: $OPENAI_API_KEY
    context_window: 128000             # 总窗口（prompt + completion），UI 算占用率用
    max_tokens: 4096                   # 单次输出上限 —— 和上面那个不是一回事
    supports_vision: true

  # - name: deepseek-reasoner
  #   use: langchain_deepseek:ChatDeepSeek
  #   model: deepseek-reasoner
  #   api_key: $DEEPSEEK_API_KEY
  #   base_url: https://api.deepseek.com   # 是 base_url 不是 api_base（见 §7.4 坑一）
  #   supports_thinking: true
  #   when_thinking_enabled:
  #     extra_body:
  #       thinking: { type: enabled }

# ------------------------------------------------------------
# 以下 section M0 还不读，留着是为了让文件形状从一开始就稳定。
# 整段注释掉会被 YAML 解析成 null，加载器当作"缺失"走默认值（见 §5.7）。
# ------------------------------------------------------------
# sandbox:         # M2
# database:        # M4   startup-only
# checkpointer:    # M4   startup-only
# summarization:   # M6
# plugins:         # M10  startup-only，且会导致代码被 import
```

**三条写模板的规矩：**

一、`startup-only` **要写在注释里**，和 §5.6 注册表里的标记对齐。改配置的人第一眼看的是这个
文件，不是 IDE 悬停提示，更不是文档。

二、**容易混淆的字段要并排注释澄清**。`context_window` / `max_tokens` 和
`base_url` / `api_base` 是两组已知会被配错的字段（§7.2、§7.4），在模板里紧挨着写清楚，
比事后在报错信息里解释便宜得多。

三、**被注释掉的示例要是能直接取消注释就跑的**。写半截的示例比没有示例更糟——用户取消注释、
补全、失败，然后不知道是自己补错了还是示例本身就不对。

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

#### `Paths` 的方法形状比实现更重要

```python
class Paths:
    def __init__(self, base_dir: str | Path | None = None):
        # 优先级：构造参数 > MYFLOW_HOME > {project_root}/.myflow
        self.base_dir = Path(base_dir).resolve() if base_dir else runtime_home()

    # --- 目录定位。user_id 一律是 keyword-only 且现在可选 ---
    def thread_dir(self, thread_id: str, *, user_id: str | None = None) -> Path: ...
    def sandbox_user_data_dir(self, thread_id: str, *, user_id: str | None = None) -> Path: ...
    def sandbox_work_dir(self, thread_id: str, *, user_id: str | None = None) -> Path: ...
    def sandbox_uploads_dir(self, thread_id: str, *, user_id: str | None = None) -> Path: ...
    def sandbox_outputs_dir(self, thread_id: str, *, user_id: str | None = None) -> Path: ...

    # --- 生命周期 ---
    def ensure_thread_dirs(self, thread_id: str, *, user_id: str | None = None) -> None: ...
    def delete_thread_dir(self, thread_id: str, *, user_id: str | None = None) -> None: ...

    # --- 虚拟路径翻译 ---
    def resolve_virtual_path(self, thread_id: str, virtual_path: str, *, user_id: str | None = None) -> Path: ...
```

`user_id` **现在就要出现在签名里，哪怕 M0 永远传** `None`**。** M9 的多用户会把布局从
`threads/{tid}/` 改成 `users/{uid}/threads/{tid}/`。参数位现在留着，那时只改实现；不留的话，
要改的是每一个调用点——而到 M9 时调用点会有几十个，分布在工具、中间件、上传路由、清理任务里。

同理，写成**方法而不是模块级常量**。常量意味着路径在 import 时就固定了，测试没法用 `tmp_path`
隔离，M9 也没法按用户分叉。

`ensure_thread_dirs` **要显式** `chmod`**，不能只靠** `mkdir(mode=...)`**。** `mkdir` 的 mode 会被进程
umask 削掉，容器里以非 root 用户跑时会撞上 "Permission denied"，而且是在第一次写文件时才报，
离创建目录很远。

#### 虚拟路径翻译要卡段边界

```python
def resolve_virtual_path(self, thread_id: str, virtual_path: str, *, user_id=None) -> Path:
    stripped = virtual_path.lstrip("/")
    prefix = VIRTUAL_PATH_PREFIX.lstrip("/")

    # 精确段边界匹配：只接受 "mnt/user-data" 本身或 "mnt/user-data/..."
    if stripped != prefix and not stripped.startswith(prefix + "/"):
        raise ValueError(f"Path must start with /{prefix}")

    relative = stripped[len(prefix) :].lstrip("/")
    resolved = (self.sandbox_user_data_dir(thread_id, user_id=user_id) / relative).resolve()
    ...  # 再做一次 is_relative_to 检查，挡 ".." 逃逸
    return resolved
```

**只写** `startswith(prefix)` **是个安全 bug。** 那样 `/mnt/user-dataX/anything` 会通过前缀检查，
然后 `stripped[len(prefix):]` 切出 `X/anything`，拼到 user-data 目录下——一个本该被拒绝的
路径变成了一个看似合法的路径。加 `+ "/"` 的那一项就是在卡段边界。

前缀检查通过之后**还要做** `..` **逃逸检查**，两者防的不是一回事：前者管"入口对不对"，后者管
"拼完之后有没有跑出去"。M2 会把这套检查扩展成完整的路径安全模块，M0 先把这两层立住。

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

M0 里唯一一个**顺序本身就是设计**的函数。下面每一条排序约束都对应一个真实故障。

#### 签名与五条覆盖通道

```python
def create_chat_model(
    name: str | None = None,
    thinking_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
    attach_tracing: bool = True,
    model_overrides: dict | None = None,
    **kwargs,
) -> BaseChatModel: ...
```

`name=None` 取 `config.models[0].name`。这是个**顺序依赖的默认值**——在 `config.yaml` 里挪动
模型顺序会静默改变默认模型。接受它（比引入 `default: true` 字段简单），但要在
`config.example.yaml` 里注明"第一个就是默认"。

`app_config` 显式注入是给测试和 M9 多租户用的，不传就取全局单例。**不要让它在函数内部自己
去 reload**，配置的生命周期归 §5.8 的单例层管。

会影响最终构造参数的有四条通道，优先级从低到高：


| 通道                                                 | 来源                  | 语义                     |
| -------------------------------------------------- | ------------------- | ---------------------- |
| `ModelConfig` 字段                                   | `config.yaml`       | 基底，`exclude_none` 之后落地 |
| `model_overrides`                                  | 调用方（M9 的自定义 agent）  | 只覆盖非 None 值            |
| `when_thinking_enabled` / `when_thinking_disabled` | `config.yaml` 的模式分支 | 覆盖上面两层                 |
| provider 特化                                        | 工厂内部                | 最后一道，可以删掉上面任何一层设的值     |


`model_overrides` **必须在思考分支和 provider 特化之前叠加**。理由是调用方可能设了
`max_tokens`，而 Codex 端点根本不接受 `max_tokens`——特化要有机会把它删掉。顺序反了的话，
调用方的覆盖就会绕过特化直接进构造函数，在请求时才报错。

`**kwargs` 是**第五条、且独立的一条**：它不并入上面那个字典，而是在最后一步和它并列展开。
这带来一条硬约束——

`kwargs` **和** `model_settings` **不能有同名 key。** 最后一步是：

```python
model_class(**kwargs, **model_settings_from_config)
```

同名会直接 `TypeError: got multiple values for keyword argument`，而且只在运行时暴露。
所以凡是工厂自己要注入或删除的 key（`reasoning_effort`、`stream_usage`、`profile`），
都必须**同时检查两个字典**，不能只看一边。

#### 完整顺序

```
 1. name 解析 → 取 ModelConfig
 2. resolve_class(use, BaseChatModel)
 3. model_dump(exclude_none=True, exclude={元数据字段})
 4. 叠加 model_overrides（忽略 None）                  ← 必须在 6、10 之前
 5. 算 effective_wte（when_thinking_enabled ⊕ thinking 快捷字段）← 在分支之外算
 6. thinking 开 / 关分支
 7. supports_reasoning_effort=false → 双向剔除 reasoning_effort
 8. api_base → base_url 别名归一                       ← 必须在 9、10、11 之前
 9. stream_chunk_timeout 注入或剔除
10. provider 特化（Codex / MindIE）
11. stream_usage 默认 True
12. 未知 key 告警
13. 构造实例
14. 构造后 merge profile（context_window → max_input_tokens）← 必须在 13 之后
15. attach_tracing
```



#### 第 3 步：exclude 集合要完整

`display_name`、`description`、`supports_`*、`when_thinking_`*、`thinking`、`context_window`、
`pricing` 全是元数据，传给 provider 客户端会被塞进 `model_kwargs` 然后在请求时炸掉。
漏掉任何一个都是个难查的 bug。

判断标准很简单：**这个字段是给人看的、给 UI 看的、还是给别的阶段看的，就必须 exclude。**
只有真正是 provider 构造参数的字段才允许落地。

#### 第 5–6 步：关掉思考比打开思考难

打开思考是"把一组参数塞进去"。关掉思考**不是"不塞"**——很多网关和模型默认就处于思考态，
不显式关就会一直思考。所以关闭必须写出一个明确的关闭参数，而这个参数的形状是 provider 方言。

第 5 步先把两个入口合成一个。`thinking` 是 `when_thinking_enabled["thinking"]` 的快捷写法，
两者都配了要合并，`thinking` 赢：

```python
has_thinking_settings = (cfg.when_thinking_enabled is not None) or (cfg.thinking is not None)
effective_wte = dict(cfg.when_thinking_enabled or {})
if cfg.thinking is not None:
    effective_wte["thinking"] = {**(effective_wte.get("thinking") or {}), **cfg.thinking}
```

**这段要算在开关分支之外。** 因为关闭分支也要读 `effective_wte`——它靠"用户是怎么写开启配置的"
来反推该用哪种方言关闭。

开启分支很短：

```python
if thinking_enabled and has_thinking_settings:
    if not cfg.supports_thinking:
        raise ValueError(f"Model {name} does not support thinking. Set `supports_thinking` to true in config.yaml.")
    model_settings.update(effective_wte)
```

注意这个 `raise` 的含义：它是**配置自相矛盾检查**（写了思考参数却没打开 `supports_thinking`），
不是能力检查。一个压根没有思考配置的模型被要求开思考，这里什么都不做——"调用方要的能力模型
不支持"该降级还是该报错，是 M1 调用侧的决定，不归工厂管。

关闭分支是四选一的 elif 链，按**从显式到推断**排列：


| 顺序  | 触发条件                                                                | 写入                                                                        |
| --- | ------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| 1   | 配了 `when_thinking_disabled`                                         | 原样 update，全权优先                                                            |
| 2   | `extra_body.thinking.type` 存在                                       | `extra_body.thinking.type = "disabled"`，外加 `reasoning_effort = "minimal"` |
| 3   | `extra_body.chat_template_kwargs` 里有 `thinking` / `enable_thinking` | 对应 key 置 `False`（vLLM 方言）                                                 |
| 4   | 顶层 `thinking.type` 存在                                               | `thinking = {"type": "disabled"}`（原生 Anthropic 方言）                        |


2–4 全部以 `has_thinking_settings` 为前提：不会思考的模型不需要关闭开关，凭空塞一个
`reasoning_effort` 反而会让不认识它的 provider 报错。

**2–4 写** `extra_body` **必须深合并，不能** `update`**。** `extra_body` 里通常还有别的网关参数，
整体替换会把它们抹掉：

```python
model_settings["extra_body"] = _deep_merge_dicts(
    model_settings.get("extra_body"), {"thinking": {"type": "disabled"}}
)
```

这套"读开启配置的形状、反推关闭写法"的做法，好处是**不需要维护一张 provider → 关闭方言的
注册表**——用户写 `when_thinking_enabled` 的时候已经把方言告诉你了。代价是遇到工厂不认识的新
方言会静默关不掉，出口就是表里第 1 行的 `when_thinking_disabled`，任何时候都能手动兜底。

#### 第 7 步：`reasoning_effort` 要双向剔除

`supports_reasoning_effort = false` 时，既要从 `model_settings` 里 pop，**也要从** `kwargs` **里 pop**。
`reasoning_effort` 是 M5 前端会直接透传下来的运行时参数（走 `**kwargs`），只清配置侧等于没清。
这是上面"两个字典都要看"那条约束的第一个实例。

#### 第 8–9 步：别名归一最先，超时默认分两种

第 8 步必须在 9、10、11 之前——下游的 OpenAI 兼容性判断要看到规范化后的 endpoint key
（细节见 §7.4 坑一）。

第 9 步有两个分支，容易只写一半：

- `BaseChatOpenAI` 子类：没配就注入一个宽松默认（DeerFlow 用 240s）。推理模型的首个 chunk
合法地要等 90~150s，langchain-openai 自带的 120s 会误杀。
- 其他 provider：**把这个 key 删掉**，不要透传。它不是这些客户端的声明字段，轻则被静默忽略
（`ChatAnthropic` 是 `extra="ignore"`），重则被塞进 `model_kwargs` 在请求时炸。两种都是用户
意图丢失，不如主动删掉。



#### 第 10 步：特化优先用基类判断

优先 `issubclass`：行为差异通常是基类带来的，用基类判断后新增的子类自动被覆盖。只有在
"这一款机型自己的毛病"或者"该类属于可选依赖、不想为了判断而 import 它"时，才退到类名匹配：

```python
if issubclass(model_class, CodexChatModel):                      # 端点特性，所有子类共享
    model_settings.pop("max_tokens", None)                       # Codex 端点拒绝 max_tokens
    ...                                                          # thinking 映射成 reasoning_effort
if getattr(model_class, "__name__", "") == "MindIEChatModel":    # 单一机型的保守默认
    model_settings.setdefault("max_retries", 1)
```

具体 provider 的 import 要**写在函数体内**。这些模块各自会拉起自己的重依赖，提到模块顶层会让
`deerflow.models` 的 import 代价跟着涨，也容易在 M1 撞上循环引用。

#### 第 14 步：profile 必须构造后 merge

把 `profile` 传给构造函数会**整体替换**掉 provider 推断出来的元数据（tool_calling、
structured_output、输出上限），只留下你设的那一个 key。正确做法是构造完再 merge：

```python
inferred = getattr(model_instance, "profile", None)
model_instance.profile = {**(inferred or {}), "max_input_tokens": model_config.context_window}
```

只在 `context_window` 有值、且调用方没有自己传 `profile` 时才做。算子显式设了窗口大小，
说明他知道推断值或缺省值对不上自己的网关，这时声明值应该赢。

#### 不要给这个函数加实例缓存

看上去很适合 `@lru_cache`——同名同参反复构造确实有开销。但这个函数**在构造之后还会改实例**：
第 14 步写 `profile`，第 15 步往 `callbacks` 里 append。缓存意味着不同调用方共享同一个可变对象，
一个 `attach_tracing=True` 的调用方会把 callback 永久留给后面 `attach_tracing=False` 的调用方，
结果就是 M10 里查不出来的重复 span。每次新建，不缓存。

#### M0 落到哪一步

15 步里 M0 真正要写的是 1–3、5–9、11–14。三个位置先留空槽：


| 步                   | M0 做法            | 补齐时机              |
| ------------------- | ---------------- | ----------------- |
| 4 `model_overrides` | 留参数，实现那三行 update | 真正的调用方在 M9        |
| 10 provider 特化      | 留位置和注释，不写具体分支    | 接第一个特殊 provider 时 |
| 15 `attach_tracing` | 留参数，函数体空         | M10               |


**参数和位置比实现更值得现在就定。** 实现可以随时补，但"4 必须在 6 之前""15 必须在 14 之后"
这两条顺序约束，等到有调用方时才发现就要回头动所有调用点了。

#### M0 完整示例（`agentkit/models/factory.py`，对着抄）

文件就这一个公开函数。辅助函数全是模块私有。`langchain_openai` 是 extra，
基类判断必须惰性 import——没装 openai extra 时 Anthropic 路径仍要能走。

```python
# agentkit/models/factory.py
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from langchain.chat_models import BaseChatModel

from agentkit.config import get_app_config
from agentkit.reflection import resolve_class

if TYPE_CHECKING:
    from agentkit.config.app_config import AppConfig

logger = logging.getLogger(__name__)

_METADATA_FIELDS = frozenset({
    "use", "name", "display_name", "description",
    "supports_thinking", "supports_reasoning_effort",
    "when_thinking_enabled", "when_thinking_disabled", "thinking",
    "supports_vision", "context_window", "pricing",
})
_DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS = 240.0


def _deep_merge_dicts(base: dict | None, override: dict) -> dict:
    merged = dict(base or {})
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _is_openai_compatible(model_class: type) -> bool:
    try:
        from langchain_openai.chat_models.base import BaseChatOpenAI
    except ImportError:
        return False
    return issubclass(model_class, BaseChatOpenAI)


def _declares_api_base(model_class: type) -> bool:
    return "api_base" in getattr(model_class, "model_fields", {})


def _vllm_disable_chat_template_kwargs(chat_template_kwargs: dict) -> dict:
    disable: dict[str, bool] = {}
    if "thinking" in chat_template_kwargs:
        disable["thinking"] = False
    if "enable_thinking" in chat_template_kwargs:
        disable["enable_thinking"] = False
    return disable


def _normalize_openai_base_url(model_class: type, settings: dict) -> None:
    """坑一：api_base → base_url。必须在超时 / stream_usage 之前。"""
    if not _is_openai_compatible(model_class) or _declares_api_base(model_class):
        return
    if "api_base" not in settings:
        return
    if "base_url" in settings or "openai_api_base" in settings:
        settings.pop("api_base", None)
        logger.warning(
            "Model config sets both an endpoint key (base_url/openai_api_base) "
            "and 'api_base'; using the former and ignoring 'api_base'."
        )
        return
    settings["base_url"] = settings.pop("api_base")


def _apply_stream_chunk_timeout_default(model_class: type, settings: dict) -> None:
    if not _is_openai_compatible(model_class):
        settings.pop("stream_chunk_timeout", None)
        return
    if "stream_chunk_timeout" in settings:
        return
    settings["stream_chunk_timeout"] = _DEFAULT_STREAM_CHUNK_TIMEOUT_SECONDS


def _warn_unknown_model_settings(model_class: type, model_name: str, settings: dict) -> None:
    """坑二：只对 OpenAI 兼容族告警。其它 provider 会误报。"""
    if not _is_openai_compatible(model_class):
        return
    known = getattr(model_class, "model_fields", None)
    if not known:
        return
    valid = set(known)
    for field in known.values():
        alias = getattr(field, "alias", None)
        if alias:
            valid.add(alias)
    valid |= {
        "model", "model_kwargs", "extra_body", "default_headers",
        "default_query", "stream_usage", "stream_chunk_timeout", "reasoning_effort",
    }
    unknown = sorted(k for k in settings if k not in valid)
    if unknown:
        logger.warning(
            "Model '%s' (%s): config key(s) %s are not recognized parameters "
            "of the model class and will be forwarded as-is; this may raise at "
            "request time. Check for typos (e.g. 'maxx_tokens' -> 'max_tokens').",
            model_name, getattr(model_class, "__name__", "?"), unknown,
        )


def create_chat_model(
    name: str | None = None,
    thinking_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
    attach_tracing: bool = True,
    model_overrides: dict | None = None,
    **kwargs: Any,
) -> BaseChatModel:
    """从 config.yaml 的 models[] 构造一个 BaseChatModel。不要给这个函数加实例缓存。"""
    # 1. name 解析。不在这里 reload；配置生命周期归单例层。
    config = app_config or get_app_config()
    if name is None:
        name = config.models[0].name
    model_config = config.get_model_config(name)
    if model_config is None:
        raise ValueError(f"Model {name} not found in config") from None

    # 2. 反射。use 是代码执行入口。
    model_class = resolve_class(model_config.use, BaseChatModel)

    # 3. 元数据字段必须全部 exclude，漏一个就会进 model_kwargs 在请求时炸掉。
    model_settings = model_config.model_dump(exclude_none=True, exclude=_METADATA_FIELDS)

    # 4. 调用方覆盖。必须在思考分支和 provider 特化之前。忽略 None。
    if model_overrides:
        model_settings.update({k: v for k, v in model_overrides.items() if v is not None})

    # 5. 合成 effective_wte。关闭分支也要读它，所以算在开关之外。
    has_thinking_settings = (
        model_config.when_thinking_enabled is not None
        or model_config.thinking is not None
    )
    effective_wte: dict = dict(model_config.when_thinking_enabled or {})
    if model_config.thinking is not None:
        effective_wte["thinking"] = {
            **(effective_wte.get("thinking") or {}),
            **model_config.thinking,
        }

    # 6. thinking 开 / 关。关不是"不塞"，是按开启配置的方言反推关闭参数。
    if thinking_enabled and has_thinking_settings:
        if not model_config.supports_thinking:
            raise ValueError(
                f"Model {name} does not support thinking. "
                "Set `supports_thinking` to true in config.yaml."
            ) from None
        model_settings.update(effective_wte)
    if not thinking_enabled:
        if model_config.when_thinking_disabled is not None:
            model_settings.update(model_config.when_thinking_disabled)
        elif has_thinking_settings and effective_wte.get("extra_body", {}).get("thinking", {}).get("type"):
            model_settings["extra_body"] = _deep_merge_dicts(
                model_settings.get("extra_body"),
                {"thinking": {"type": "disabled"}},
            )
            model_settings["reasoning_effort"] = "minimal"
        elif has_thinking_settings and (
            disable := _vllm_disable_chat_template_kwargs(
                effective_wte.get("extra_body", {}).get("chat_template_kwargs") or {}
            )
        ):
            model_settings["extra_body"] = _deep_merge_dicts(
                model_settings.get("extra_body"),
                {"chat_template_kwargs": disable},
            )
        elif has_thinking_settings and effective_wte.get("thinking", {}).get("type"):
            model_settings["thinking"] = {"type": "disabled"}

    # 7. 两个字典都要 pop。reasoning_effort 会从 M5 走 **kwargs 进来。
    if not model_config.supports_reasoning_effort:
        kwargs.pop("reasoning_effort", None)
        model_settings.pop("reasoning_effort", None)

    # 8–9. 别名归一必须在超时 / stream_usage 之前。
    _normalize_openai_base_url(model_class, model_settings)
    _apply_stream_chunk_timeout_default(model_class, model_settings)

    # 10. provider 特化。M0 留槽；具体分支等接到第一个特殊 provider 再写。
    #     import 写在函数体内，不要提到模块顶层。
    # if issubclass(model_class, CodexChatModel):
    #     model_settings.pop("max_tokens", None)
    # if getattr(model_class, "__name__", "") == "MindIEChatModel":
    #     model_settings.setdefault("max_retries", 1)

    # 11. 坑三：第三方 endpoint 不会默认 stream_usage=True。两个字典都要看。
    if "stream_usage" not in model_settings and "stream_usage" not in kwargs:
        if "stream_usage" in getattr(model_class, "model_fields", {}):
            model_settings["stream_usage"] = True

    # 12. 未知 key 告警（非致命）。
    _warn_unknown_model_settings(model_class, name, model_settings)

    # 13. 同名 key 会 TypeError，所以工厂注入/删除的 key 必须同时检查两个字典。
    translate_context_window = (
        bool(model_config.context_window)
        and "profile" not in kwargs
        and "profile" not in model_settings
    )
    model_instance = model_class(**kwargs, **model_settings)

    # 14. 构造后再 merge。传进构造函数会整体替换推断出来的 profile。
    if translate_context_window:
        inferred = getattr(model_instance, "profile", None)
        model_instance.profile = {
            **(inferred or {}),
            "max_input_tokens": model_config.context_window,
        }

    # 15. M0 函数体空。M1 图内调用必须传 attach_tracing=False；M10 才真正挂 callbacks。
    if attach_tracing:
        pass

    return model_instance
```



调用方（M0 验收 / 手工试模型）：

```python
from agentkit.models.factory import create_chat_model

model = create_chat_model("gpt-4o")          # name=None 则取 config.models[0]
reply = model.invoke("ping")
```

M1 构图时的正确调用（现在就把 `attach_tracing=False` 写上，即使第 15 步还是空的）：

```python
model = create_chat_model(
    name=resolved_config.models[0].name,
    thinking_enabled=False,
    app_config=resolved_config,
    attach_tracing=False,
)
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


| 测试文件                           | 覆盖                                                                                                                                                                                                           |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `test_harness_boundary.py`     | harness 不 import app                                                                                                                                                                                         |
| `test_harness_packaging.py`    | provider 包在 optional-dependencies 里                                                                                                                                                                          |
| `test_app_config.py`           | 四级路径优先级；显式路径缺失抛异常 vs 搜索模式返回 None；`$ENV` 解析成功/缺失抛异常；空文件/null section 降级为默认；版本检查先于 `$ENV` 解析（缺环境变量时报的是版本问题）；`$ENV` 解析先于校验（`port: $DB_PORT` 能转成 int）；零模型只 warning 不抛；版本过低 warning；名称索引首匹配语义                     |
| `test_app_config_singleton.py` | `set_app_config` 注入后磁盘变更**不会**顶掉 mock；签名变更触发重载；路径变更触发重载；ContextVar 覆盖优先于进程单例且不读磁盘；push/pop 嵌套还原；空栈 pop 不抛；**两个并发 context 各自 push 互不可见**（守住元组栈）                                                               |
| `test_config_signature.py`     | 同秒等长改内容能被检出；mtime 回退能被检出；文件不存在返回 None；stat 成功但读失败返回部分签名                                                                                                                                                      |
| `test_reload_boundary.py`      | 注册表 ↔ schema description 双向一致                                                                                                                                                                                |
| `test_paths.py`                | 虚拟路径映射；`/mnt/user-dataX/evil` **被拒**（段边界，不是纯前缀）；`/mnt/user-data/../../etc` 被拒（逃逸）；`/mnt/user-data` 自身合法；非法 user_id / thread_id 被拒；`make_safe_user_id` 不碰撞；Windows 路径拼接保留风格；`MYFLOW_PROJECT_ROOT` 指向不存在路径时抛异常 |
| `test_reflection.py`           | 正常解析；路径格式错误；模块缺失给出安装提示；模块内部 ImportError 保留原信息；`expected_type` 校验；`base_class` 校验                                                                                                                             |
| `test_model_factory.py`        | 见下                                                                                                                                                                                                           |


模型工厂的测试用一个 fake `BaseChatModel` 子类，不要真连 provider。要覆盖的点按 §7.3 的顺序：

- 元数据字段（`display_name` / `supports_*` / `context_window` / `pricing`）一个都不进构造函数
- `model_overrides` 忽略 None；且它设的值仍会被 provider 特化删掉（顺序断言，不只是结果断言）
- 思考**关闭**的四条分支各一例：显式 `when_thinking_disabled`、`extra_body` 方言、vLLM
`chat_template_kwargs` 方言、原生 `thinking` 方言；外加"无思考配置时四条都不触发"
- 关闭分支写 `extra_body` 时，同级的其他网关参数**不被抹掉**（深合并）
- `thinking` 快捷字段与 `when_thinking_enabled` 同时存在时的合并方向
- `supports_thinking=false` 却配了思考参数 → 请求开思考时抛异常
- `supports_reasoning_effort=false` 时，`kwargs` 和配置两侧的 `reasoning_effort` 都被清掉
- `api_base` 被改名为 `base_url`；两个 endpoint key 同时存在时保留规范的那个并告警
- `stream_chunk_timeout`：OpenAI 族注入默认值且尊重显式值，非 OpenAI 族被剔除
- 未知 key 告警只对 OpenAI 族触发
- `stream_usage` 默认开，且 `kwargs` 里显式给了就不再注入
- `context_window` 在构造**之后** merge 进 profile，不替换 provider 推断出的其他 key
- 同参数连调两次返回**两个不同实例**（守住"不加缓存"这条约定）

---



## 11. 验收清单

- [ ] `uv sync` 从零安装成功，且**不含任何 LLM provider 包**
- [ ] `uv sync --extra openai` 后 `create_chat_model("gpt-4o")` 能造出实例
- [ ] 未装 provider 时的报错包含 `uv add langchain-xxx` 的可执行提示
- [ ] 改 `config.yaml` 后**不重启**，下一次 `get_app_config()` 拿到新值
- [ ] 同一秒内把配置改成等长的不同内容，同样能被检出
- [ ] `cp config.example.yaml config.yaml` 后直接启动不崩（全注释的 section 走默认值）
- [ ] `MYFLOW_CONFIG_PATH` 指向不存在的文件 → 明确报错，不静默降级
- [ ] 删掉 `.env` 里的 `OPENAI_API_KEY` → 报"环境变量不存在"，而不是拿空 key 去请求
- [ ] `set_app_config(mock)` 之后改磁盘配置，拿到的仍然是 mock
- [ ] 请求一个 `/mnt/user-dataX/x` 路径 → 被拒
- [ ] 在 harness 里写一句 `from app.x import y` → CI 红
- [ ] 给一个 startup-only 字段漏掉 description 前缀 → CI 红
- [ ] 单测乱序跑（`pytest -p no:randomly` 关掉后再开）结果一致 —— 验证配置隔离 fixture 生效
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