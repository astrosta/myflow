from agentkit.config.stream_config import StreamBridgeConfig
from agentkit.runtime.stream_bridge.memory import MemoryStreamBridge
from agentkit.runtime.stream_bridge.base import StreamBridge

def create_stream_bridge(config: StreamBridgeConfig) -> StreamBridge:
    """按 backend 选实现。这个函数是 redis 依赖的唯一 import 点。"""
    if config.backend == "redis":                                    # M9
        from agentkit.stream_bridge.redis import RedisStreamBridge    # 懒加载：没装 redis 也不影响 M3
        return RedisStreamBridge(config)
    return MemoryStreamBridge(
        queue_maxsize=config.queue_maxsize,
        heartbeat_interval=config.heartbeat_interval_seconds,
    )