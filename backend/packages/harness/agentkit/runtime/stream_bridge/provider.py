from agentkit.config import StreamBridgeConfig
from agentkit.runtime.stream_bridge.base import StreamBridge
from agentkit.runtime.stream_bridge.memory import MemoryStreamBridge


def create_stream_bridge(config: StreamBridgeConfig, *, gateway_workers: int = 1) -> StreamBridge:
    """按 backend 选实现。这个函数是 redis 依赖的唯一 import 点。

    M9 §3.5：多 worker 必须 redis。memory 是进程本地的，跨进程订不到同一条 run
    ——客户端会死等一条永远没有帧的流，所以这里直接拒绝启动，不降级运行。
    """
    if gateway_workers > 1 and config.backend != "redis":
        raise RuntimeError(
            "GATEWAY_WORKERS > 1 requires stream_bridge.backend=redis; "
            "the memory bridge cannot carry frames across processes"
        )
    if config.backend == "redis":
        # 懒加载：没装 redis 也不影响 memory 后端；缺失时启动阶段就报错，不等第一帧
        from agentkit.runtime.stream_bridge.redis import RedisStreamBridge

        return RedisStreamBridge.from_config(config)
    return MemoryStreamBridge(
        queue_maxsize=config.queue_maxsize,
        heartbeat_interval=config.heartbeat_interval_seconds,
    )