
STARTUP_ONLY_PREFIX = "startup-only:"

STARTUP_ONLY_FIELDS: dict[str,str] = {
    "log_level": (
        "apply_logging_level() 只在启动时运行；重载后的AppConfig不会重新触发它"
    ),
    "checkpointer.type": (
        "get_checkpointer() 首次调用时按 type 创建单例；重载后的 AppConfig 不会重建它"
    ),
    "database.backend": "init_engine() / make_checkpointer() 在 lifespan 里绑死",
    "database.sqlite_dir": "SQLite 文件路径在引擎创建时解析为绝对路径",
    "database.postgres_url": "连接串在引擎和 checkpointer 池创建时捕获",
    "database.postgres_schema": "search_path 钉在 connect_args 上",
    "database.checkpoint_channel_mode": "编译进图的通道表，跨进程必须一致",
    "database.pool_size": "ORM 连接池在 create_async_engine 时定容",
    "run_events.backend": "make_run_event_store() 在 lifespan 里构造一次",
    "stream_bridge.backend": "create_stream_bridge() 在 lifespan 里构造一次；换后端要重建单例",
    "observability.langfuse": "Langfuse client / exporter 线程在 lifespan 里构造一次；换地址或开关要重启进程",
    "sandbox.use": "get_sandbox_provider() 首次调用时按 use 创建单例；重载后的 AppConfig 不会重建它",
    "sandbox.cube": "CubeSandbox provider 与 lease 在首次获取时按配置构造；重载后的 AppConfig 不会重建它们",
    "sandbox.opensandbox": "OpenSandbox provider 与 lease 在首次获取时按配置构造；镜像、sandbox 与服务端地址都绑在这个单例上",
    "skills.container_path": "它被编译进系统提示词的技能索引和沙箱 mount；换路径要重建沙箱视图",
    "skills.builtin.path": "内置技能目录在 Sandbox provider 构造时固化为只读挂载；换路径要重启进程",
    "skills.builtin.enabled": "内置技能开关与只读挂载一起在 Sandbox provider 构造时冻结",
    # M8 §4.2：进程级准入控制器在 Gateway 启动时构建；热重载改 YAML 不会重建它。
    # 提示词和 Limit 中间件读的是同一个冻结值，不能让热重载把容量宣告得比控制器更大。
    "subagent_runtime.max_running": (
        "进程级准入控制器在 Gateway 启动时构建；热重载改 YAML 不会重建它。"
    ),
    "subagent_runtime.max_queued": (
        "队列容量与控制器一起冻结。"
    ),
    "subagent_runtime.admission_policy": (
        "queue/reject 是控制器构造参数。"
    ),
    "subagent_runtime.queue_timeout_seconds": (
        "排队超时是控制器构造参数。"
    ),
}

def format_field_description(field_path: str, *, field_doc: str|None=None) -> str:
    reason = STARTUP_ONLY_FIELDS[field_path]
    header = f"{STARTUP_ONLY_PREFIX} {reason}"
    return header if field_doc is None else f"{header}\n{field_doc.strip()}"