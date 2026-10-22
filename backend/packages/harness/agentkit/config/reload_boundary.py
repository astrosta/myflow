
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
    "memory.backend_class": (
        "get_memory_manager() 单例按 backend_class 构造；换后端必须先 reset_memory_manager()"
    ),
    "database.pool_size": "ORM 连接池在 create_async_engine 时定容",
    "run_events.backend": "make_run_event_store() 在 lifespan 里构造一次",
    "stream_bridge.backend": "create_stream_bridge() 在 lifespan 里构造一次；换后端要重建单例",
    "sandbox.use": "get_sandbox_provider() 首次调用时按 use 创建单例；重载后的 AppConfig 不会重建它",
    "skills.container_path": "它被编译进系统提示词的技能索引和沙箱 mount；换路径要重建沙箱视图",
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