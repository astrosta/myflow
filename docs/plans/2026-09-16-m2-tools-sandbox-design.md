# M2 工具与沙箱详细设计

> 配套文档：[分阶段开发方案](2026-09-16-greenfield-agent-service-roadmap.md) ·
> [M0 地基](2026-09-16-m0-foundation-design.md) · [M1 最小 Agent](2026-09-16-m1-minimal-agent-design.md) ·
> [路由层详细设计](2026-09-16-router-layer-design.md)
>
> 本文细化 M2（2 周）。沿用包名：框架层 `agentkit.*`，应用层 `app.*`。

---

## 1. M2 的范围

**做什么：** 让 agent 能在受控环境里读写文件、执行命令，并把三类工具来源汇聚成一个入口。
浏览器进出这些文件只走 §10 的四条路由。

**M2 的核心命题只有一句：现在只实现本地沙箱，但接口必须按"沙箱在另一台机器上"来设计。**

这不是过度设计。本地 provider 之后会长出 Docker、E2B、micro-VM、K8s provisioner 四五种实现，
它们的共同点是：网络往返、可能失败、有生命周期、有并发所有权。任何一处泄漏了"沙箱在本进程里"
的假设——同步方法、返回 `[]` 表示失败、直接用 `pathlib` 拼路径——都会在接第二个 provider 时暴露。

### 三层边界

```
┌─────────────────────────────────────────────┐
│ 工具层 tools/       模型可见的 schema、参数校验、  │
│                     输出格式化、纵深防御的路径检查   │
├─────────────────────────────────────────────┤
│ 沙箱抽象 sandbox/   Sandbox（做什么）              │
│                     SandboxProvider（生命周期）    │
├─────────────────────────────────────────────┤
│ 实现层 sandbox/local/  路径映射、进程管理、错误归一   │
└─────────────────────────────────────────────┘
```

**不做什么：**

| 不做 | 因为 |
|---|---|
| Docker / 远程 provider | M9。M2 只保证接口能容纳它 |
| 跨实例所有权租约 | M9。单进程内的锁是够的 |
| 温池（warm pool）、空闲回收 | 本地沙箱没有创建成本 |
| 沙箱授权门禁 | M9 有了鉴权再加，但调用位置现在留出来 |
| 技能视图挂载 | M7 |

### 两周排期

| 天 | 内容 |
|---|---|
| D1–2 | `Sandbox` / `SandboxProvider` 抽象 + 错误契约 + provider 单例 |
| D3–4 | `LocalSandboxProvider` + 路径映射 + 路径安全 |
| D5–6 | 文件工具：`read_file` / `write_file` / `str_replace` / `ls` / `glob` / `grep` |
| D7 | `bash` 工具：进程组、超时、后台进程、输出捕获 |
| D8 | 工具三源汇聚 + 沙箱生命周期中间件 + 上传/产物路由（§10） |
| D9–10 | 测试：路径逃逸、并发写、错误契约、跨平台 |

### M2 新增的文件

```
agentkit/sandbox/
├── __init__.py                     # 只 re-export
├── sandbox.py                   ★  # Sandbox ABC + 错误契约（契约写在 docstring 里）
├── sandbox_provider.py          ★  # SandboxProvider ABC + 进程单例
├── exceptions.py                ★  # SandboxError 家族，工具层按类型翻译
├── security.py                     # is_host_bash_allowed / uses_local_sandbox_provider
├── env_policy.py                   # build_sandbox_env（密钥擦除）
├── path_patterns.py                # 输出反向遮蔽
├── file_operation_lock.py          # (sandbox_id, path) 锁
├── search.py                       # glob / grep 的共享实现
│                                   # SandboxMiddleware（生命周期）已迁到
│                                   # agentkit/agents/middlewares/sandbox_middleware.py
├── runtime.py                   ★  # ensure_sandbox_initialized —— 工具拿沙箱的唯一入口
├── tools/
│   ├── __init__.py                 # 导出工具对象供 config 的 use: 引用
│   ├── file_tools.py               # read/write/str_replace/ls/glob/grep
│   └── bash_tool.py                # 单独一个文件，它最复杂
└── local/
    ├── __init__.py
    ├── local_sandbox.py            # Sandbox 实现：路径翻译、进程管理
    ├── local_sandbox_provider.py   # 映射表构建、每线程实例缓存
    └── list_dir.py                 # 树形渲染

agentkit/tools/registry.py          # get_available_tools 三源汇聚（M1 建的空壳，M2 填实现）

app/gateway/
├── thread_access.py                # require_visible_thread，threads 路由与 §10 共用
├── thread_files.py              ★  # 上传落盘、列表、删除、打开产物；路由不碰路径
├── models/response.py              # 追加 UploadResponse / UploadedFileInfo
├── errors.py                       # §10.4 的六个异常
└── routers/
    ├── uploads.py               ★  # POST/GET list/DELETE uploads
    └── artifacts.py             ★  # GET artifacts/{path}
```

**工具不要全塞进一个 `tools.py`。** DeerFlow 的对应文件长到了 2661 行，里面混着工具定义、
沙箱获取、密钥注入、错误脱敏、Windows 编码处理。从一开始就按"文件工具 / bash / 运行时管道"
分三块，边界和后面的中间件层是对齐的。

特别是 `runtime.py` 要独立：它是**工具层和沙箱层之间唯一的桥**，M9 的授权门禁装在这一个文件里。
混在工具文件里，门禁就会被复制成 N 份。

---

## 2. Sandbox 抽象（D1–2）

### 2.1 接口

```python
class Sandbox(ABC):
    _id: str

    @property
    def id(self) -> str: ...

    @abstractmethod
    def execute_command(self, command: str, env: dict[str, str] | None = None,
                        timeout: float | None = None) -> str: ...

    @abstractmethod
    def read_file(self, path: str, start_line: int | None = None,
                  end_line: int | None = None) -> str: ...

    @abstractmethod
    def write_file(self, path: str, content: str, append: bool = False) -> None: ...

    @abstractmethod
    def download_file(self, path: str) -> bytes: ...

    @abstractmethod
    def update_file(self, path: str, content: bytes) -> None: ...

    @abstractmethod
    def list_dir(self, path: str, max_depth: int = 2) -> list[str]: ...

    @abstractmethod
    def glob(self, path: str, pattern: str, *, include_dirs: bool = False,
             max_results: int = 200) -> tuple[list[str], bool]: ...

    @abstractmethod
    def grep(self, path: str, pattern: str, *, glob: str | None = None,
             literal: bool = False, case_sensitive: bool = False,
             max_results: int = 100) -> tuple[list[GrepMatch], bool]: ...
```

三个设计点：

**文本读写和二进制读写分开。** `read_file`/`write_file` 处理文本（支持行范围、追加），
`download_file`/`update_file` 处理字节。远程 provider 的这两类走完全不同的传输路径
（一个可能走 shell，一个必须走文件 API），合并成一个方法会逼着实现层做类型分支。

**有界操作返回 `(结果, 是否被截断)`。** `glob` 和 `grep` 可能匹配上万条，必须有上限。
返回元组而不是只返回列表，是为了让工具层能告诉模型"还有更多，请缩小范围"——
否则模型会以为它看到了全部。

**`execute_command` 接受 per-call `env`。** 这是给 M9 的请求级密钥用的（短期 token 传给脚本，
不经过提示词和工具参数）。M2 用不上，但接口位现在留着比以后加参数容易。

### 2.2 错误契约是抽象层的主要内容

**这是 M2 最容易被忽略、也最值得写进 docstring 的部分。** 不同 provider 的失败方式完全不同
（本地是 `OSError`，远程是 HTTP 错误码，容器是退出码），抽象层必须规定统一的翻译规则，
否则工具层要为每个 provider 写一套错误处理。

| 方法 | 情况 | 必须抛/返回 |
|---|---|---|
| `list_dir` | 路径不存在 | `FileNotFoundError` |
| `list_dir` | 命令/客户端失败 | `OSError` |
| `list_dir` | 目录存在但为空 | `[]` |
| `download_file` | 路径逃逸 | `PermissionError` |
| `download_file` | 读不了/不存在 | `OSError` |
| `execute_command` | `env` key 非法 | `ValueError` |

**`list_dir` 那三行是重点。** 失败时返回 `[]` 是最自然也最错的写法——工具层会把它渲染成
`(empty)`，模型看到"目录是空的"，然后基于这个错误前提继续推理。**失败和空必须可区分。**

远程实现还有一个具体陷阱：用 `find ... | head -n N` 做截断时，`sh -lc` 默认没有 `pipefail`，
`$?` 拿到的是 `head` 的状态而不是 `find` 的。必须显式捕获 `find` 的退出码，
同时把截断导致的 SIGPIPE 当成成功。

### 2.3 能力声明用三态，默认 fail closed

provider 是通过类路径动态加载的，可能来自第三方。任何"这个实现有没有某种性质"的问题，
都不能用 `bool` 默认值回答：

```python
class Sandbox(ABC):
    #: execute_command 是否复用持久 shell 会话（export、cwd、函数跨调用存活）。
    #: 三态且 fail closed：None = 实现没有声明。第三方 provider 可能复用会话，
    #: 所以"沉默"不能读作"每次都是干净 shell"。
    #: 消费方只信显式的 False，遇到 None 要和 True 一样降级处理。
    persistent_shell_sessions: bool | None = None
```

M2 只有本地实现（`False`），这个字段看起来多余。但它的消费方（M10 的执行证据验证）
需要知道"这条命令的环境能不能从命令文本本身证明是干净的"。等到那时才加字段，
所有已有的第三方 provider 都会默认落进错误的那一侧。

---

## 3. SandboxProvider 抽象（D1–2）

### 3.1 接口与 async 规则

```python
class SandboxProvider(ABC):
    uses_thread_data_mounts: bool = False

    @abstractmethod
    def acquire(self, thread_id: str | None = None, *, user_id: str | None = None) -> str: ...

    async def acquire_async(self, thread_id=None, *, user_id=None) -> str:
        """默认实现把同步版本丢到线程池。异步运行时必须调这个。"""
        return await asyncio.to_thread(self.acquire, thread_id, user_id=user_id)

    @abstractmethod
    def get(self, sandbox_id: str) -> Sandbox | None: ...

    @abstractmethod
    def release(self, sandbox_id: str) -> None: ...

    def reset(self) -> None: ...
```

**规则一：所有异步调用方必须走 `*_async`。** 本地 provider 的 `acquire` 是瞬时的，
所以 M2 里这条规则没有可观测效果。但 Docker 的 `acquire` 要创建容器、轮询就绪、
拿跨进程锁——那是几秒的阻塞 IO。如果调用方写的是同步 `acquire()`，整个事件循环会卡住，
所有并发会话一起卡。

这条规则要用测试钉住，而不是靠 code review。加一个 blocking-IO 检测套件
（`blockbuster` 之类），在异步测试里注入一个故意阻塞的 provider，
任何在事件循环上直接调同步方法的代码路径都会失败。

**规则二：`get()` 必须是纯内存查找。** 它在工具的热路径上被高频调用，
一旦它做了任何 IO（查所有权存储、探活、读文件），就会变成事件循环上的阻塞调用。
所有权、健康检查这类信息必须在 `acquire` 时确定并缓存。

### 3.2 单例：锁只保护引用交换

```python
_default_provider: SandboxProvider | None = None
_provider_lock = threading.Lock()


def get_sandbox_provider(**kwargs) -> SandboxProvider:
    # 快路径：加锁读，防止并发 reset 在检查和返回之间把它置空
    with _provider_lock:
        if _default_provider is not None:
            return _default_provider

    # 冷启动：在锁外 resolve + 构造。
    # 这两步是插件代码（config.sandbox.use 指向任意类），可能很慢，
    # 甚至可能重入这些生命周期函数 —— 在非重入锁里跑会自死锁。
    config = get_app_config()
    provider = resolve_class(config.sandbox.use, SandboxProvider)(**kwargs)

    with _provider_lock:
        if _default_provider is None:
            _default_provider = provider
            return provider
        winner = _default_provider

    # 输了安装竞争：把刚造出来的那个拆掉。
    # 有副作用构造函数的 provider（比如启动了后台线程）不拆会泄漏。
    if hasattr(provider, "shutdown"):
        provider.shutdown()
    return winner
```

三件事值得注意：**锁只保护全局引用的读写**，插件回调（`__init__` / `reset` / `shutdown`）
和动态 import 一律在锁外；**输了竞争的那个实例要主动销毁**，否则它的后台线程会一直跑；
**快路径也要加锁读**，裸读会在并发 reset 时拿到 `None`。

这个 provider 会被多个 OS 线程访问（主事件循环、M9 的 IM 通道线程），不是理论风险。

### 3.3 `reset()` 和 `shutdown()` 的语义差别

- `reset()`：清理跨实例存活的缓存状态，让下一次 `acquire` 重新生效。**不保证释放活跃沙箱。**
- `shutdown()`：完整销毁，释放所有沙箱。

M2 里本地 provider 两者差不多，但**语义现在就要分开写在 docstring 里**。远程 provider
的 `reset()` 如果去销毁远程容器，测试里的一次配置切换就会把别的进程正在用的容器干掉。

### 3.4 sandbox_id 要自带作用域

`get()` 必须是纯内存查找（§3.1 规则二），这就要求 **id 本身携带足够的信息定位实例**，
不能只是个随机 UUID 再去查表——查表意味着表要么在内存里（那还不如把作用域编进 id），
要么在存储里（那就违反了"不做 IO"）。

```python
def _sandbox_id_for_thread(thread_id: str, user_id: str) -> str:
    return f"local:{user_id}:{thread_id}"

def _key_from_sandbox_id(sandbox_id: str) -> tuple[str, str] | None:
    """反解回 (user_id, thread_id)；格式不符返回 None，不抛异常。"""
    if not sandbox_id.startswith("local:"):
        return None
    user_id, sep, thread_id = sandbox_id[len("local:") :].partition(":")
    return (user_id, thread_id) if sep and user_id and thread_id else None
```

`local:` 前缀还有第二个用途：工具层要判断"当前是不是本地沙箱"（决定要不要建目录、
要不要做 Windows 路径转换），靠前缀匹配就够，不用回头问 provider。

**同时保留一个无 thread 上下文的通用实例**（id 就是 `"local"`）。CLI、脚本、测试这些场景
没有 thread，强制要求 thread_id 会让它们被迫编造一个。判断本地沙箱时两种形式都要认：
`sandbox_id == "local" or sandbox_id.startswith("local:")`。

#### 每线程实例要有上限

这是个容易漏的内存泄漏：**长驻 Gateway 里 thread_id 的数量是无界的**。每个
`LocalSandbox` 实例本身很便宜（一个小对象加一个映射列表），但无界增长就是无界增长。

```python
self._thread_sandboxes: OrderedDict[tuple[str, str], LocalSandbox] = OrderedDict()
self._max_cached_threads = 256      # 超出时在下一次 acquire 驱逐 LRU
```

用 `OrderedDict` 做 LRU，`acquire` 时 `move_to_end`，超限时 `popitem(last=False)`。

**驱逐的是缓存，不是沙箱本身。** 本地沙箱没有需要销毁的远程资源，被驱逐的实例下次
`acquire` 时重建即可，磁盘上的文件不受影响。远程 provider 不能照抄这个策略——那里
驱逐必须伴随真正的释放，否则容器会泄漏。所以这个上限属于 `LocalSandboxProvider` 的实现细节，
**不要提到 `SandboxProvider` 抽象里**。

---

## 4. 虚拟路径系统（D3–4）

### 4.1 映射表

| agent 看到 | 物理位置 |
|---|---|
| `/mnt/user-data/workspace` | `.myflow/users/{uid}/threads/{tid}/user-data/workspace` |
| `/mnt/user-data/uploads` | `.../uploads` |
| `/mnt/user-data/outputs` | `.../outputs` |
| `/mnt/skills` | 技能目录（M7） |

**映射表在 `acquire` 时构建，绑定到那个 sandbox 实例。** 不要做成全局函数——
不同 thread 的映射不同，不同 provider 的映射机制也不同（本地是路径翻译，
Docker 是 volume mount，E2B 是上传）。

#### 数据结构与两层来源

```python
@dataclass(frozen=True)
class PathMapping:
    container_path: str    # agent 看到的虚拟路径，如 /mnt/user-data/workspace
    local_path: str        # 宿主实际路径
    read_only: bool = False
```

映射分两层来源，**构建时机不同**：

| 层 | 构建时机 | 内容 | 为什么 |
|---|---|---|---|
| 静态 | provider `__init__` | 公共技能目录、`config.yaml` 里的自定义 mount | 进程级不变，每个沙箱都一样 |
| 每线程 | `acquire(thread_id, user_id=)` | `/mnt/user-data/*`、per-user 技能 | 依赖 thread_id 和 user_id |

**静态那层绝不能包含任何依赖 `user_id` 的映射。** 这是个已经踩过的坑：在 `__init__` 里调
`get_effective_user_id()` 绑定"当前用户"的自定义技能目录，之后**所有其他用户的沙箱**都会
解析到第一个用户的目录。凡是 per-user 的，一律推迟到 `acquire`。

#### 解析规则：最长前缀获胜

映射之间**允许重叠**，查找时按 `container_path` 长度降序匹配第一个命中的：

```python
def _find_path_mapping(self, virtual_path: str) -> PathMapping | None:
    for mapping in sorted(self._mappings, key=lambda m: len(m.container_path), reverse=True):
        if virtual_path == mapping.container_path or virtual_path.startswith(mapping.container_path + "/"):
            return mapping
    return None
```

这条规则让一个**聚合父映射**成为可能，而它是必需的：

```python
PathMapping("/mnt/user-data",           ".../user-data"),            # 父，聚合
PathMapping("/mnt/user-data/workspace", ".../user-data/workspace"),  # 子，更长，优先
PathMapping("/mnt/user-data/uploads",   ".../user-data/uploads"),
PathMapping("/mnt/user-data/outputs",   ".../user-data/outputs"),
```

没有那条父映射，`ls /mnt/user-data` 会因为找不到映射而报错——而在 Docker 里那是个真实目录，
`ls` 能列出三个子目录。**两种 provider 的行为必须一致**，否则模型在本地学会的探索习惯
换到容器就失效。加一条父映射就抹平了这个差异，代价是必须有最长前缀规则来保证子路径仍走子映射。

注意匹配同样要卡**段边界**（`== prefix` 或 `startswith(prefix + "/")`），理由和 M0 §6.1 一样：
纯 `startswith` 会让 `/mnt/user-dataX` 命中 `/mnt/user-data` 的映射。这个检查在 M0 的
`Paths` 和 M2 的 provider 里各出现一次，**两处都要有**——它们防的是不同入口。

### 4.2 翻译发生在哪一层

这是个容易做乱的地方。正确的分工是：

- **provider 的映射表是权威。** 它在 `acquire` 时确定，是"这个沙箱能看到什么"的唯一真相源。
- **工具层只解析 `/mnt/user-data/...`**，作为纵深防御和参数校验。技能路径、
  自定义挂载这些保持虚拟，交给 provider 的映射表处理。

两层都做翻译听起来冗余，但工具层那层的价值是**在路径到达沙箱之前就拒绝非法输入**，
并且给出清晰的错误信息。provider 层那层则保证即使工具层漏了，边界依然成立。

**Docker 实现天然一致**：容器里就挂在同样的虚拟路径上，所以两种实现都原生接受
`/mnt/user-data/...`，不需要任何条件分支。这是虚拟路径这个设计最大的回报。

### 4.3 输出要做反向遮蔽

工具返回给模型的内容里，宿主机的真实路径必须被换回虚拟路径。否则模型会学到
`/home/deploy/.myflow/users/u_123/threads/...` 这种路径，然后在下一轮里直接用它——
本地 provider 会接受（因为 bash 能访问），换到 Docker 就全挂。

实现上有个性能坑：**不要为每个 thread 编译一条路径正则**。Python 的 `re` 有全局缓存，
被驱逐的 thread id 会一直留在缓存里。正确做法是直接扫描动态的 per-user/per-thread 根路径，
只有进程内稳定的那一小撮（技能目录、集成目录）才用编译缓存。

---

## 5. 路径安全（D3–4）

### 5.1 校验清单

每一个进入沙箱的路径都要过这几关，且**在做任何文件系统操作之前**：

- `..` 路径穿越（包括 URL 编码、多重编码）
- 符号链接逃逸（解析后再比对前缀，不能只看字符串）
- 绝对路径逃逸（不在允许的虚拟前缀下的绝对路径）
- Windows 盘符（`C:\`）、UNC 路径（`\\server\share`）
- `file://` 协议
- 空字节

被拒绝时的错误信息要给**可操作的恢复指引**，而不是干巴巴的 "permission denied"：
如果模型在探测环境，告诉它用纯命令探测；如果它要访问文件，告诉它允许的虚拟路径是哪些。
而且**不要在错误信息里重复被拒绝的那个路径**——模型很容易把它当成"再试一次"的提示。

#### 检查顺序：先纯字符串，再查映射，最后才碰文件系统

```python
def resolve_sandbox_path(virtual_path: str, mappings: list[PathMapping]) -> Path:
    # --- 第一组：纯语法，零 IO，最便宜也最容易绕过 ---
    if "\x00" in virtual_path:
        raise PermissionError(_REJECT_HINT)
    if "://" in virtual_path:                       # file:// 及其他协议
        raise PermissionError(_REJECT_HINT)
    if re.match(r"^[A-Za-z]:[\\/]", virtual_path) or virtual_path.startswith("\\\\"):
        raise PermissionError(_REJECT_HINT)         # 盘符 / UNC
    if not virtual_path.startswith("/"):
        raise PermissionError(_REJECT_HINT)         # 必须是绝对虚拟路径

    # --- 第二组：查映射，仍然零 IO ---
    mapping = _find_path_mapping(virtual_path, mappings)   # 最长前缀，卡段边界
    if mapping is None:
        raise PermissionError(_REJECT_HINT)

    relative = virtual_path[len(mapping.container_path) :].lstrip("/")

    # --- 第三组：触碰文件系统，最贵，放最后 ---
    root = Path(mapping.local_path).resolve()
    candidate = (root / relative).resolve()         # resolve() 会跟随符号链接
    if not candidate.is_relative_to(root):
        raise PermissionError(_REJECT_HINT)         # ".." 和符号链接逃逸一起挡在这里
    return candidate
```

**顺序不是随意的。** 三组检查的成本差两个数量级：第一组是几次字符串比较，第三组的
`resolve()` 是系统调用，且在网络挂载上可能很慢。把便宜的放前面，绝大多数恶意输入在零 IO
的情况下就被拒了。反过来写，一个构造出来的深层路径能让每次拒绝都付出真实的磁盘代价。

**`..` 和符号链接由同一行挡住。** 很多实现会先写个 `if ".." in path` 的字符串检查，然后
以为搞定了——那挡不住符号链接，也挡不住 `a/b/../../..` 这种被正常化后才越界的路径。
正确做法是**先 `resolve()` 再比前缀**，`is_relative_to` 一行同时覆盖两类逃逸。字符串层的
`..` 检查可以作为快速拒绝加在第一组，但**不能替代**第三组。

**`_REJECT_HINT` 是个常量，不插值被拒的路径。** 除了上面说的"模型会拿它再试一次"，
还有个理由：统一的错误文本让所有拒绝路径产生同一条消息，模型更容易学会"这类路径不行"
而不是逐个试探。提示内容应该是正向的——列出允许的虚拟前缀，而不是描述哪里错了。

#### 一个必须承认的窗口：resolve 和 open 之间不是原子的

`resolve()` 检查通过之后到真正 `open()` 之间，符号链接可以被换掉（TOCTOU）。要彻底关掉
这个窗口需要 `openat` + `O_NOFOLLOW` 这类平台相关的原子操作。

M2 **不做**这件事，理由是 §5.2 那条：本地沙箱本来就不是安全边界，在一个非边界上做原子性
加固是错配的投入。真正需要隔离的场景走 Docker provider，那里边界由内核提供。
但这个窗口要写在 docstring 里——**已知的缺口和未知的缺口，运维成本差很远。**

### 5.2 本地沙箱不是安全边界，这件事要说清楚

这是 M2 必须做的一个诚实声明：**`LocalSandboxProvider` 提供的是路径规范化，不是隔离。**

原因很简单：一旦启用了宿主 `bash`，模型可以直接用规范路径访问任何东西，
完全绕过 `PathMapping`。文件工具的边界是真的，shell 的边界是假的。

所以要有一个显式开关，且**默认关闭**：

```yaml
sandbox:
  use: agentkit.sandbox.local:LocalSandboxProvider
  allow_host_bash: false   # 默认；仅在完全可信的本地环境设为 true
```

```python
def is_host_bash_allowed(config=None) -> bool:
    if not uses_local_sandbox_provider(config):
        return True          # 真正隔离的 provider 不受此限制
    return bool(config.sandbox.allow_host_bash)
```

关掉时，`get_available_tools()` 直接把 bash 类工具从列表里过滤掉，
并且错误信息要告诉用户怎么办（"切到 Docker provider，或者在可信环境显式打开"）。

对应地，provider 要有一个动态的能力声明：

```python
@property
def supports_agent_skill_isolation(self) -> bool:
    """宿主 bash 开着时返回 False —— shell 能绕过路径映射。"""
    return not is_host_bash_allowed()
```

M7 的技能隔离策略会读这个标志并 fail closed。**"这个 provider 能不能强制执行隔离"
必须是可查询的，不能靠文档约定。**

### 5.3 环境变量策略

`execute_command` **不能直接继承整个 `os.environ`**。Gateway 进程里有 `OPENAI_API_KEY`、
数据库密码、各种 token，这些不该让技能脚本看到。

```python
_SECRET_NAME_PATTERNS = ("KEY", "SECRET", "TOKEN", "PASS", "CREDENTIAL")

def build_sandbox_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """擦掉继承环境里名字像密钥的变量，再叠加显式注入的请求级密钥。"""
    base = {k: v for k, v in os.environ.items()
            if not any(p in k.upper() for p in _SECRET_NAME_PATTERNS)}
    base.update(extra or {})
    return base
```

保留良性变量（`PATH`、`HOME`、`LANG`、`VIRTUAL_ENV`）。名字匹配是粗糙的启发式，
但它挡住的是最常见的泄漏路径，成本几乎为零。

`env` 的 key 要校验成合法 POSIX 变量名（`^[A-Za-z_][A-Za-z0-9_]*$`）。
当前实现都是结构化传递（`subprocess.run(env=...)`、容器 API 的 env 字段），不拼 shell 字符串，
所以这是纯纵深防御。但**把规则放在抽象层**，未来某个实现真的去拼 shell 时不用自己重新推导。

---

## 6. 文件工具（D5–6）

### 6.1 工具怎么拿到沙箱：唯一入口 `ensure_sandbox_initialized`

工具函数通过注入的 `runtime` 拿到状态和上下文，沙箱不是参数传进来的，而是从状态里解出来的。
**这条链路必须只有一个实现**，因为 M9 的授权门禁、M9 的租约、M2 的懒初始化全都挂在这里。

```python
def ensure_sandbox_initialized(runtime: Runtime | None = None) -> Sandbox:
    if runtime is None or runtime.state is None:
        raise SandboxRuntimeError("Tool runtime not available")

    # 1. 授权检查在复用和新建之前都要跑（见下方说明）
    if not _AUTHORIZATION_CHECKED.get():
        authorize_sandbox_execution(context=runtime.context or {}, app_config=safe_app_config())

    # 2. 状态里已有就复用
    sandbox_state = runtime.state.get("sandbox")
    if sandbox_state and (sandbox_id := sandbox_state.get("sandbox_id")):
        sandbox = get_sandbox_provider().get(sandbox_id)
        if sandbox is not None:
            runtime.context["sandbox_id"] = sandbox_id      # 供 after_agent 释放
            return sandbox
        # get() 返回 None 说明已被释放，落到下面重新获取

    # 3. 懒初始化
    thread_id = _resolve_runtime_thread_id(runtime)
    if thread_id is None:
        raise SandboxRuntimeError("Thread ID not available in runtime context")

    provider = get_sandbox_provider()
    sandbox_id = provider.acquire(thread_id, user_id=resolve_runtime_user_id(runtime))
    runtime.state["sandbox"] = {"sandbox_id": sandbox_id}   # 跨工具调用存活

    sandbox = provider.get(sandbox_id)
    if sandbox is None:
        _rollback_failed_acquire(provider, sandbox_id)      # 见下
        raise SandboxNotFoundError("Sandbox not found after acquisition", sandbox_id=sandbox_id)

    runtime.context["sandbox_id"] = sandbox_id
    return sandbox
```

**四条不能省的细节：**

**一、授权必须在复用路径上也检查，不能只在 acquire 时。** `sandbox_id` 一旦写进
`ThreadState`，它会在整个会话里被复用。如果只在首次 acquire 时鉴权，那么角色被撤销、
策略被改之后，这个 thread 仍然能一直用下去——权限变更对进行中的会话完全无效。
**授权是执行时策略，不是 sandbox id 的终身属性。** M2 的 `authorize_sandbox_execution`
是个空实现，但调用位置现在就放对。

用一个 ContextVar 做单次运行内的去重（`_AUTHORIZATION_CHECKED`），避免同一轮里每个工具
都重跑一次鉴权——它可能要查数据库。

**二、thread_id 的解析顺序是 `context` 优先，`config.configurable` 兜底。**

```python
def _resolve_runtime_thread_id(runtime: Runtime) -> str | None:
    if runtime.context and (tid := runtime.context.get("thread_id")):
        return tid
    if runtime.config:
        return runtime.config.get("configurable", {}).get("thread_id")
    return None
```

两个来源都要查：`configurable` 是 M1 定的图调用契约，`context` 是 M8 子代理会覆盖的那一份。
只查后者，子代理场景会拿到父线程的 id。

**三、写回要写两个地方，用途不同。** `runtime.state["sandbox"]` 让沙箱跨工具调用存活
（它进 checkpoint，走 M1 那个 fail-closed 的 `merge_sandbox`）；`runtime.context["sandbox_id"]`
是给 `after_agent` 的释放路径用的，不进 checkpoint。漏掉后者，沙箱不会被释放。

**四、acquire 成功但 `get()` 返回 None 时必须回滚。** 这是个窄窗口（并发 reset、驱逐），
但不回滚就会泄漏一次获取——本地 provider 只是缓存条目，远程 provider 就是一个没人认领的容器。
**先回滚，再抛异常。**

#### 同步和异步两份，异步路径必须用异步那份

```python
async def ensure_sandbox_initialized_async(runtime) -> Sandbox:
    ...
    sandbox_id = await provider.acquire_async(thread_id, user_id=...)
```

逻辑完全镜像，唯一区别是走 `acquire_async` 和异步版鉴权。这是 §3.1 规则一在工具层的落地：
**异步工具调同步版本，会在 Docker provider 下把整个事件循环卡几秒。** 两份实现容易漂移，
所以要有一条测试断言两者的行为一致（同样的 runtime 输入得到同样的 sandbox_id 和同样的状态写回）。

同步工具在异步图里执行时，走"先异步初始化沙箱、再把同步工具体扔进线程池"的组合：

```python
async def _run_sync_tool_after_async_init(func, runtime, *args):
    await ensure_sandbox_initialized_async(runtime)
    return await asyncio.to_thread(func, runtime, *args)
```

### 6.2 工具清单与语义要点

| 工具 | 要点 |
|---|---|
| `read_file` | 支持行范围（1-indexed，闭区间）；大文件要截断并告知 |
| `write_file` | 默认覆盖；`append` 作为模型可见参数暴露；自动建父目录 |
| `str_replace` | 子串替换；**匹配不唯一必须报错**，不要替换第一处 |
| `ls` | 树形输出，最多两层；空目录渲染成 `(empty)` |
| `glob` | 按模式找文件/目录，有上限，返回是否截断 |
| `grep` | 单文件或递归目录，可选 glob 过滤，行级结果有上限 |

**`str_replace` 的唯一匹配规则是硬要求。** 模型给的 `old_string` 匹配到多处时，
替换第一处会造成静默的错误修改，而且模型不知道。正确做法是报错并告诉它匹配了几处、
让它提供更多上下文。

**每个工具都带一个模型可见的 `description` 字段**，用作前端的人类可读进度标签
（"正在读取配置文件"）。但**工具执行绝不能依赖这个字段**——它是可选的，默认空串，
前端在缺失时用本地化的兜底文案。把它做成必填或让逻辑依赖它，会让第三方工具很难接。

#### 一个工具长什么样

```python
@tool
def read_file(
    path: str,
    runtime: Runtime,
    start_line: int | None = None,
    end_line: int | None = None,
    description: str = "",
) -> str:
    """Read a text file from the sandbox.

    Args:
        path: Absolute virtual path, e.g. /mnt/user-data/workspace/main.py
        start_line: 1-indexed first line to read (inclusive).
        end_line: 1-indexed last line to read (inclusive).
        description: Optional short label shown in the UI.
    """
    try:
        sandbox = ensure_sandbox_initialized(runtime)
        content = sandbox.read_file(path, start_line=start_line, end_line=end_line)
        return mask_host_paths(content, sandbox)     # §4.3 反向遮蔽
    except SandboxError as e:
        return f"Error: {e}"
    except Exception as e:
        return f"Error: {_sanitize_error(e, runtime)}"
```

**工具返回错误字符串，不向外抛异常。** 这是 M2 要立的一条全局约定。工具抛异常会中断
agent 循环，而绝大多数工具错误（文件不存在、路径非法、命令失败）是模型**可以自己恢复**的——
它应该看到错误、改参数、重试。让它看到的方式就是把错误变成返回值。

注意这和 §2.2 的错误契约不矛盾，两者是分层的：**沙箱层抛类型化异常，工具层捕获并翻译成
字符串**。沙箱层的类型是给代码看的（工具层要能区分"不存在"和"读失败"），字符串是给模型看的。
M6 会在这上面再加一层结构化元数据（`recoverable_by_model` 等），届时工具层的返回会从
裸字符串升级成带元数据的结果——**所以现在不要让任何调用方去解析这个字符串的格式。**

**兜底的 `except Exception` 要过脱敏。** 未预期异常的消息里可能带宿主路径、连接串、
环境变量片段。`_sanitize_error` 至少要做 §4.3 的路径遮蔽，M9 加密钥遮蔽时也在这一处。

**`runtime` 参数不出现在模型看到的 schema 里**，它由框架注入。这意味着 docstring 里
**不要给它写 Args 说明**——那段文字会进模型的函数描述，白白占 token 还可能让模型尝试传它。

### 6.3 同路径写操作要串行化，锁的 key 是 `(sandbox_id, path)`

同一个 graph step 里两个工具可能同时写一个文件。需要一把按路径的锁：

```python
_FILE_LOCKS: weakref.WeakValueDictionary[tuple[str, str], threading.Lock] = weakref.WeakValueDictionary()
_GUARD = threading.Lock()


def get_file_operation_lock(sandbox: Sandbox, path: str) -> threading.Lock:
    key = (sandbox.id or f"instance:{id(sandbox)}", path)
    with _GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[key] = lock
        return lock
```

两个细节都不能省：

**key 必须包含 `sandbox.id`，不能只用 path。** 不同 thread 的沙箱里有同名虚拟路径
（`/mnt/user-data/workspace/main.py` 在每个会话里都存在）。只用 path 做 key，
两个互相隔离的会话会在同一把锁上互相阻塞。

**必须用 `WeakValueDictionary`。** 长驻进程里路径是无限的（每个 thread 每个文件一个 key），
普通 dict 会无限增长。弱引用让最后一个持有者释放后锁自动消失。

### 6.4 Read-before-write 的钩子位

M6 会加"写之前必须先读过"的门禁（防止模型凭空覆盖它没看过的文件）。M2 不实现，
但 `write_file` 和 `str_replace` 的调用路径要留出**前置检查 + 后置标记**的位置，
并且这两个钩子要能拿到文件的哈希。

---

## 7. bash 工具（D7）

bash 是单个最复杂的工具，本地实现尤其。

### 7.1 必须处理的五件事

**超时与进程组。** 命令要在自己的进程组里跑，带墙钟超时（`sandbox.bash_command_timeout`，
默认 600s）。超时后杀掉**整个进程组**（POSIX）或整棵进程树（Windows），不能只杀直接子进程——
`npm install` 起的一堆子进程会变成孤儿。

**stdin 必须是 `/dev/null`。** 否则任何读 stdin 的命令（交互式确认、`read`）会永久挂起，
而且看起来像是模型卡住了。给它立即 EOF。

**后台进程不能阻塞这一轮。** `python server.py &` 这种命令，如果子进程继承了管道，
父进程读管道会一直等到子进程退出——也就是永远。POSIX 上要让后台进程立即返回，
同时把未重定向的后台输出排掉（不能让匿名临时文件无限增长）。

**输出捕获要用有界的管道排空线程。** 直接 `subprocess.run(capture_output=True)` 在输出很大时
会把内存吃光，而且在管道满时会死锁。

**工具描述里要提前告诉模型把长命令放后台。** 这是 prompt 层的优化，但收益很大：
不说的话模型会用前台起服务器，然后白白等掉整轮的超时。

### 7.2 Windows 的两个差异

如果要支持 Windows 开发环境（大概率要，M2 的开发机就是）：

编码上，Windows 用平台文本编码解码并做通用换行转换（处理 locale code page、
Python UTF-8 Mode、CRLF、裸 CR）。**这个转换只在 Windows 做**，POSIX 保持字节解码不改写换行，
否则会破坏已有的输出契约。

Git Bash/MSYS 的路径参数自动转换要精确排除：只排除安全的非根虚拟路径前缀，
**不要整体关掉转换**——宿主的原生 CLI 需要正常的 MSYS 路径转换才能找到自己的安装目录。

---

## 8. 工具三源汇聚（D8）

### 8.1 组装顺序与去重

```python
def get_available_tools(
    groups: list[str] | None = None,
    include_mcp: bool = True,
    model_name: str | None = None,
    subagent_enabled: bool = False,
    *,
    app_config: AppConfig | None = None,
) -> list[BaseTool]:
```

组装顺序即优先级：**配置工具 → 内置工具 → MCP 工具**，按 `tool.name` 去重，先到先得。

重名必须告警并跳过，不能让两个同名工具都进 schema——模型会收到歧义的或被拼接的函数定义，
然后调用时报 "not a valid tool"。这类问题排查起来很费劲，因为报错发生在模型侧。

### 8.2 配置里的 `name` 和工具对象的 `.name` 不一致要告警

```python
if cfg.name != loaded.name:
    logger.warning(
        "Tool name mismatch: config name %r != tool .name %r (use: %s). "
        "The tool's own .name will be used for binding.",
        cfg.name, loaded.name, cfg.use,
    )
```

这是个真实的坑：模型收到的 schema 用的是 `.name`，但运行时路由如果按配置的 `name` 找，
两边对不上。统一以 `.name` 为准，但要吼一声，因为这通常是配置写错了。

### 8.3 能力开关

工具列表要按三类条件过滤：

| 条件 | 影响 |
|---|---|
| `is_host_bash_allowed()` | 本地 provider + 未显式允许 → 去掉 bash 类工具 |
| `model_config.supports_vision` | 不支持 → 去掉 `view_image` |
| `subagent_enabled` | 关闭 → 去掉 `task`（M8） |
| `non_interactive` | 调度器场景 → 去掉 `ask_clarification`（M9） |

M2 只需要前两条，但**过滤要集中在一处**而不是散落在各个调用点。
M9 加授权过滤时只在这里插一个 `apply_tool_authorization()`。

### 8.4 工具配置 schema

```python
class ToolConfig(BaseModel):
    name: str
    group: str          # 逻辑分组，供 groups 参数过滤
    use: str            # 变量路径，如 "agentkit.sandbox.tools:bash_tool"
    model_config = ConfigDict(extra="allow")
```

`use` 指向**变量**（已实例化的工具对象）而不是类，所以用 M0 的 `resolve_variable`
而不是 `resolve_class`。

---

## 9. 沙箱生命周期中间件（D8）

一个 `SandboxMiddleware`，职责是在 agent 开始前 acquire、结束后 release，
并把 `sandbox_id` 写进 `ThreadState.sandbox`（M1 定义的那个带 fail-closed reducer 的字段）。

两个要点：

**acquire 要有懒初始化的备选路径。** 不是每次运行都会碰沙箱（纯问答就不会），
提前 acquire 在 Docker 场景下是几秒的浪费。正确做法是中间件做预热，
同时工具层有一个 `ensure_sandbox_initialized()` 的兜底入口——
这个入口后来也会成为 M9 授权门禁的唯一检查点。

**release 不能由单个工具触发。** 一个 step 里可能有多个工具在用同一个沙箱，
任何一个结束就 release 会把兄弟工具的沙箱抽掉。释放必须由外层生命周期（整个 graph 执行结束）
负责。M9 会把这个演化成引用计数的租约管理器，M2 先用最简单的"中间件 acquire、中间件 release"。

---

## 10. 上传与产物路由（D8）

路由层 §3.2 把四条路由划给 M2。错误体、能力门、`ThreadId`、静态段顺序沿用路由层，不另写一套。
`create_app` 的 `include_router` 在路由层 §7.1。本节只定义 handler 和落盘。

这四条路由是浏览器和沙箱之间的口。路径翻译只用 §5.1 的 `resolve_sandbox_path`，router 里不出现 `Path(...) / filename`。

### 10.1 路由表

| Method | Path | 请求 | 成功响应 | 能力 | 所属文件 |
| --- | --- | --- | --- | --- | --- |
| POST | `/api/threads/{thread_id}/uploads` | `multipart` 字段 `files` | `UploadResponse`（201） | `threads:write` | `app/gateway/routers/uploads.py` |
| GET | `/api/threads/{thread_id}/uploads/list` | 无 | `UploadListResponse`（200） | `threads:read` | 同上 |
| DELETE | `/api/threads/{thread_id}/uploads/{filename}` | 路径参数 | `DeletedUploadResponse`（200） | `threads:write` | 同上 |
| GET | `/api/threads/{thread_id}/artifacts/{path:path}` | `?download=` | 文件字节，无 `response_model` | `threads:read` | `app/gateway/routers/artifacts.py` |

两个 router 的 `prefix` 都是 `/threads`，`create_app` 再加 `/api`。

`GET .../uploads/list` 必须写在 `DELETE .../uploads/{filename}` 前面。后写的话，`DELETE /uploads/list` 仍会命中 `{filename}`，那是允许的（文件就可以叫 `list`）。不能加的是 `GET /uploads/{filename}`：它会把 `list` 吃掉，列表接口变成 404。上传文件不从 HTTP 按名字下载，agent 用虚拟路径 `/mnt/user-data/uploads/{filename}` 去读。

不分页。一个 thread 的上传目录有上限（§10.2），列表就是这一个目录的全集。套 `Page` 会让客户端以为后面还有一页。

### 10.2 参数

```python
# app/gateway/routers/uploads.py
UploadFilename = Annotated[str, Path(min_length=1, max_length=255)]
```

`UploadFilename` 不靠正则收口。合法文件名含中文和空格；正则要么漏掉它们，要么挡不住 `..`。形状检查在 `app/gateway/thread_files.py::check_upload_filename`，DELETE 和 POST 共用。

| 输入 | 结果 | `code` |
| --- | --- | --- |
| 空串、超过 255、含 `/` `\` 或 NUL、名为 `.` 或 `..`、含 `..` 这一段 | 422 | `invalid_upload` |
| 通过检查但目录里没有这个文件（DELETE） | 404 | `upload_not_found` |
| `thread_id` 不是 `ThreadId` | 422 | `invalid_request`（路由层 §7.2，进不了函数） |

`thread_id` 的类型是 `app/gateway/models/request.py` 的 `ThreadId`。属主检查调用 `app/gateway/thread_access.py::require_visible_thread`，不在本文件再写一次 `thread_store.get`。行不存在或不是这个 caller 的，都是 404 `thread_not_found`。所以上传之前要先 `POST /api/threads`。

```yaml
# config.yaml
uploads:
  max_files_per_request: 10
  max_files_per_thread: 100
  max_file_size_bytes: 10485760      # 10 MiB
  max_total_size_bytes: 33554432     # 单次请求合计
```

| 配置 | 超过时 | `code` |
| --- | --- | --- |
| `max_file_size_bytes` / `max_total_size_bytes` | 413 | `upload_too_large` |
| `max_files_per_request` | 422 | `invalid_upload` |
| `max_files_per_thread` | 409 | `upload_limit_reached` |

大小在读流时累计，不是先把 body 收完再量。到顶立刻停，并删掉已经写下的临时文件。

产物路径是 URL 里的 `{path:path}`，没有前导斜杠。服务层先拼成 `"/"` + `path`，再跑 §5.1。只允许落在 `/mnt/user-data/outputs` 下面（最长前缀，卡段边界）。`uploads` 和 `workspace` 不从这条路由读。

| 产物路径 | 结果 | `code` |
| --- | --- | --- |
| 空字节、`://`、盘符、UNC、不是绝对虚拟路径、不在 `outputs` 下、`resolve` 之后逃出根 | 422 | `invalid_artifact_path` |
| 语法合法，文件不存在，或解析结果是目录 | 404 | `artifact_not_found` |

422 的 `detail` 用 §5.1 的 `_REJECT_HINT`，不把客户端传来的路径拼进去。

`download` 是 `Query` 上的 `bool`，默认 `false`。

### 10.3 响应

```python
# app/gateway/models/response.py
class UploadedFileInfo(BaseModel):
    filename: str                       # 落盘名。重名时带 _N 后缀
    original_filename: str              # 客户端原始文件名（已通过 check_upload_filename）
    size: int
    virtual_path: str                   # 恒为 /mnt/user-data/uploads/{filename}
    modified: str                       # ISO-8601，Z 结尾


class UploadResponse(BaseModel):
    files: list[UploadedFileInfo]


class UploadListResponse(BaseModel):
    files: list[UploadedFileInfo]
    count: int


class DeletedUploadResponse(BaseModel):
    deleted: str                        # 被删的 filename，不是虚拟路径
```

响应里没有宿主机路径。`path`、`artifact_url` 都不出现：前者会把 `.myflow/users/...` 漏给浏览器（违反 §4.3），后者让客户端依赖一条还没承诺的下载 URL。

同一次请求里两个文件净化后同名，后一个在扩展名前加 `_2`、`_3`。`report.pdf` 第二次变成 `report_2.pdf`。后缀加在扩展名前面，避免把 `.pdf` 弄丢。

产物响应是 `FileResponse`，不是 JSON。

| 头 | 值 | 谁加 |
| --- | --- | --- |
| `Content-Type` | 按扩展名的小表；不认识就是 `application/octet-stream` | `app/gateway/thread_files.py::open_artifact` |
| `Content-Disposition` | `inline`，或 `attachment` | 见下 |
| `X-Trace-Id` | 当前 trace | 路由层 §12 的中间件 |

`text/html`、`application/xhtml+xml`、`image/svg+xml` 永远是 `attachment`，不管 `download`。这三类在浏览器里会执行。其余类型只在 `download=true` 时用 `attachment`。`filename` 只用 basename，并按 RFC 5987 编码，不把虚拟路径写进头里。

### 10.4 handler

```python
# app/gateway/routers/uploads.py
router = APIRouter(prefix="/threads", tags=["uploads"])


@router.get("/{thread_id}/uploads/list", response_model=UploadListResponse)
async def list_uploads(
    thread_id: ThreadId,
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> UploadListResponse:
    await require_visible_thread(services, thread_id, caller)   # app/gateway/thread_access.py
    files = await list_upload_dir(thread_id, caller.user_id)    # app/gateway/thread_files.py
    return UploadListResponse(files=files, count=len(files))


@router.post("/{thread_id}/uploads", status_code=201, response_model=UploadResponse)
async def create_uploads(
    thread_id: ThreadId,
    files: Annotated[list[UploadFile], File()],
    caller: Annotated[RunCaller, Depends(require_scope("threads:write"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> UploadResponse:
    await require_visible_thread(services, thread_id, caller)
    if not files:
        raise UploadInvalid("At least one file is required")
    saved = await save_uploads(thread_id, caller.user_id, files)
    return UploadResponse(files=saved)


@router.delete("/{thread_id}/uploads/{filename}", response_model=DeletedUploadResponse)
async def delete_upload(
    thread_id: ThreadId,
    filename: UploadFilename,
    caller: Annotated[RunCaller, Depends(require_scope("threads:write"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> DeletedUploadResponse:
    await require_visible_thread(services, thread_id, caller)
    check_upload_filename(filename)                              # 不通过则 UploadInvalid
    await delete_upload_file(thread_id, caller.user_id, filename)
    return DeletedUploadResponse(deleted=filename)
```

`GET /list` 的声明在 `POST` 和 `DELETE` 前面。FastAPI 按声明顺序匹配。

```python
# app/gateway/routers/artifacts.py
router = APIRouter(prefix="/threads", tags=["artifacts"])


@router.get("/{thread_id}/artifacts/{path:path}")
async def get_artifact(
    thread_id: ThreadId,
    path: str,
    download: Annotated[bool, Query()] = False,
    caller: Annotated[RunCaller, Depends(require_scope("threads:read"))],
    services: Annotated[RunServices, Depends(get_run_services)],
) -> FileResponse:
    await require_visible_thread(services, thread_id, caller)
    opened = await open_artifact(thread_id, caller.user_id, path, download=download)
    return opened.response
```

这条没有 `response_model`。路由层 §13.2 的白名单要加上 `("GET", "/api/threads/{thread_id}/artifacts/{path:path}")`。

| 步骤 | 上传 | 列表 / 删除 | 产物 |
| --- | --- | --- | --- |
| 1 | `threads:write` 在进函数之前 | 列表是 `threads:read`，删除是 `threads:write` | `threads:read` |
| 2 | `require_visible_thread` | 同左 | 同左 |
| 3 | `save_uploads`：校验、限流、临时文件、改名 | 列表只读目录；删除先 `check_upload_filename` 再删 | `open_artifact`：§5.1，然后确认是普通文件 |
| 4 | `UploadResponse` | `UploadListResponse` 或 `DeletedUploadResponse` | `FileResponse` |

```python
# app/gateway/thread_files.py
async def save_uploads(thread_id: str, user_id: str | None, files: list[UploadFile]) -> list[UploadedFileInfo]:
    """读和写都放进 asyncio.to_thread。路由在事件循环上，目录 IO 不在。

    每个文件先写入同目录的 .upload-{uuid}.part，大小和总数都过了再 rename。
    中途失败删掉已写的 part，已改名的文件也删掉，这次请求要么全进目录，要么一个都不留。
    """
```

虚拟路径是 `/mnt/user-data/uploads/{filename}`。物理位置由该 thread 的 `PathMapping` 决定（§4.1），`user_id` 来自 `caller`，不来自表单字段。

六个异常放在 `app/gateway/errors.py`，不继承 `StartRunError`。登记方式和 M1 的 `ModelNotFound` 一样：类上带 `status_code` 和 `code`，走 `_handle_domain_error`。

| 异常 | HTTP | `code` | 谁抛 |
| --- | --- | --- | --- |
| `UploadInvalid` | 422 | `invalid_upload` | 文件名、空列表、单次个数 |
| `UploadTooLarge` | 413 | `upload_too_large` | 单文件或单次合计超限 |
| `UploadLimitReached` | 409 | `upload_limit_reached` | 该 thread 已有 `max_files_per_thread` 个文件 |
| `UploadNotFound` | 404 | `upload_not_found` | DELETE 时目录里没有 |
| `ArtifactPathInvalid` | 422 | `invalid_artifact_path` | §5.1 拒绝，或前缀不是 `outputs` |
| `ArtifactNotFound` | 404 | `artifact_not_found` | 路径合法但不是一个现存文件 |

### 10.5 这条路由不做什么

| 不做 | 理由 |
| --- | --- |
| 上传时把 PDF / Office 转成 Markdown | 转换器失败会让「文件已收到」和「目录里有什么」对不上。M2 原样落盘 |
| `PUT` 改产物、`Range` 断点 | 路由层没把它们划给 M2。读是整文件 |
| 从 `.skill` 压缩包里抽一个成员 | 那是技能包的读法，不是产物文件 |
| 在路由里 `chmod` 或拼宿主机路径 | 权限和映射属于 provider。HTTP 层只交虚拟路径和字节 |
| 上传进一个还没有 thread 行的 id | 404。否则磁盘上会出现一条没有任何元数据的目录 |


---

## 11. 测试清单（D9–10）

| 文件 | 覆盖 |
|---|---|
| `test_path_security.py` | `..` 穿越、符号链接逃逸、绝对路径逃逸、Windows 盘符、UNC、`file://`、空字节，每种都断言被拒且错误信息不回显被拒路径 |
| `test_sandbox_contract.py` | **错误契约**：`list_dir` 缺失路径抛 `FileNotFoundError`、失败抛 `OSError`、空目录返回 `[]`；非法 `env` key 抛 `ValueError` |
| `test_local_sandbox.py` | 虚拟路径双向翻译；输出反向遮蔽；不同 thread 的同名路径互不干扰；**最长前缀**（`/mnt/user-data/workspace/x` 走子映射，`ls /mnt/user-data` 走父映射）；`/mnt/user-dataX` 不命中；**per-user 映射不被 `__init__` 绑死**（用户 A 建 provider、用户 B 的沙箱解析到 B 的目录）；每线程实例超上限后 LRU 驱逐且重建无损 |
| `test_sandbox_runtime.py` | `ensure_sandbox_initialized` 的四条路径：状态复用、懒初始化、`get()` 返回 None 后重建、acquire 成功但 `get()` 失败时**回滚再抛**；`state` 和 `context` 两处都写回；thread_id 优先取 `context` 再取 `configurable`；**授权在复用路径上也被调用**；sync 与 async 两版对同一输入产生相同的 sandbox_id 和状态写回 |
| `test_file_tools.py` | `str_replace` 多处匹配报错；`write_file` 追加 vs 覆盖；读文件行范围；截断标志正确 |
| `test_file_lock.py` | 同沙箱同路径并发写串行化；**不同沙箱同路径不阻塞**；锁在无引用后被回收 |
| `test_bash_tool.py` | 超时杀整个进程组；stdin 立即 EOF；后台进程不阻塞；大输出不 OOM |
| `test_tools_assembly.py` | 三源去重与优先级；重名告警；name 不一致告警；宿主 bash 默认被过滤 |
| `blocking_io/test_sandbox_async.py` | 异步路径不调同步 `acquire`；`get()` 不做 IO |
| `test_uploads_router.py` | 空 `files` → 422 `invalid_upload`；`../x` 与含 `/` 的文件名 → 422，detail 不含该路径；超限 → 413 / 409；同名第二次得到 `report_2.pdf`；响应 JSON 不含宿主机路径；`GET .../uploads/list` 不被当成文件名；别人的 thread → 404 `thread_not_found` |
| `test_artifacts_router.py` | `outputs` 下的文件 200；`uploads` 与 `workspace` 前缀 → 422 `invalid_artifact_path`；`..` 逃逸 → 422 且 detail 是 `_REJECT_HINT`；缺文件 → 404；`.html` / `.svg` 的 `Content-Disposition` 是 `attachment` |

最后那个 blocking-IO 套件是 M2 引入的新测试层，**建议单独一条 CI gate**。
它在后面每个阶段都会救命，因为"在事件循环上偷偷做了阻塞 IO"这类 bug 在功能测试里完全看不出来，
只在生产的并发压力下表现为莫名其妙的延迟尖峰。

---

## 12. 验收清单

- [ ] 模型在沙箱里写 Python 脚本并 `bash` 执行成功
- [ ] `../../etc/passwd`、`C:\Windows\System32`、指向外部的符号链接全部被拒，错误信息可操作
- [ ] `list_dir` 对不存在的路径抛异常，对空目录返回 `[]`，工具层渲染成 `(empty)`
- [ ] 两个 thread 同时写各自的 `workspace/main.py`，互不阻塞且内容正确
- [ ] `str_replace` 的 `old_string` 匹配两处时报错，且告知匹配数
- [ ] 前台跑 `sleep 999` 在超时后被杀，整个进程组清干净，没有僵尸
- [ ] `python -m http.server &` 立即返回，不占满整轮
- [ ] 默认配置下 bash 工具不在工具列表里；显式 `allow_host_bash: true` 后出现
- [ ] 工具返回的内容里不含任何宿主机真实路径
- [ ] 任何工具错误都以 `Error: ...` 字符串返回，agent 循环不中断，模型能改参数重试
- [ ] `ls /mnt/user-data` 列出三个子目录（和 Docker 行为一致），而不是报"找不到映射"
- [ ] 用户 A 启动进程、用户 B 发起会话，B 的沙箱解析到 B 自己的目录
- [ ] blocking-IO 套件通过
- [ ] `POST .../uploads` 写入的文件，agent 能用 `/mnt/user-data/uploads/{filename}` 读到，响应里没有宿主机路径
- [ ] `GET .../artifacts/...` 只能读 `outputs`；`uploads`、`..`、不存在的文件分别是 422、422、404

---

## 13. 刻意不做的事

| 不做 | 理由 |
|---|---|
| Docker / 远程 provider | M9。但 M2 结束时要能回答"接 Docker 需要改哪些文件"，答案应该只有"新增一个 provider 实现" |
| 跨实例容器所有权 | M9。那是一整套租约状态机，单机开发阶段付不起这个复杂度 |
| 温池 / 空闲回收 | 本地沙箱创建成本为零，没有池化的意义 |
| 沙箱授权门禁 | M9。但 `ensure_sandbox_initialized()` 这个唯一入口现在就要有，届时门禁装在那一处 |
| 命令执行作用域（scope）| M8 的并发子代理才需要隔离 shell 会话。接口上留 `execute_command_in_scope` 的默认透传实现即可 |
| 网络策略 / 出口管控 | M9 之后。本地 provider 没有网络边界可言 |

---

## 14. 一句话总结

M2 写的代码里，90% 是本地实现，10% 是抽象——但价值分布正好相反。

判断 M2 做得对不对，只需要一个检验：**把 `LocalSandboxProvider` 整个删掉换成 Docker 实现，
工具层、agent 层和 §10 的四条路由需要改几行？** 答案应该是零。路由只认虚拟路径。
