from typing import Literal

from pydantic import BaseModel, Field, model_validator

from agentkit.config.reload_boundary import format_field_description

# XREAD BLOCK 占着 socket，Redis 要等阻塞结束才回空包。客户端的读超时必须更晚，
# 否则 redis-py 会在 read_response 里把这次正常空等报成 TimeoutError。
# 多出来的 5 秒盖住回包的网络延迟和事件循环抖动。
_BLOCK_TIMEOUT_SLACK_SECONDS = 5.0


class StreamBridgeConfig(BaseModel):
    """直播帧桥。进程级单例，lifespan 里按 backend 构造一次（M3 §7.5 / M9 §3.6）。"""

    backend: Literal["memory", "redis"] = Field(
        default="memory",
        description=format_field_description("stream_bridge.backend"),
    )
    queue_maxsize: int = Field(default=256, gt=0)  # 每 run 保留的数据事件数，决定"能重放多久"
    heartbeat_interval_seconds: float = Field(default=15.0, ge=0)  # 0 = 关掉心跳（只给嵌入式用）

    # ---- 以下只在 backend=redis 时生效（M9 §3.6 / §7）----
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description=(
            "redis://[[username]:password@]host:port/db。密码请用 $REDIS_URL 从环境变量注入，"
            "不要写进版本库；日志只打印抹掉密码的形态。"
        ),
    )
    stream_ttl_seconds: float = Field(
        default=3600.0,
        gt=0,
        description=(
            "retained key 的 TTL，每次 publish / publish_end 刷新。"
            "它是泄漏安全网，不是 run 超时：run 的正常收口靠 publish_end + cleanup。"
        ),
    )
    tombstone_ttl_seconds: float = Field(
        default=300.0,
        gt=0,
        description="cleanup 之后『这条 run 已回收』这个事实保留多久；期间订阅立刻拿到 END。",
    )
    key_prefix: str = Field(default="myflow:stream", min_length=1)
    socket_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        description=(
            "读超时，必须至少比 read_block_seconds 多 5 秒。"
            "XREAD BLOCK 在空闲时会占满整个阻塞时间，相等的客户端超时会先触发 TimeoutError。"
        ),
    )
    socket_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    health_check_interval_seconds: float = Field(default=30.0, ge=0)
    max_connections: int = Field(default=32, gt=0)
    read_block_seconds: float = Field(
        default=5.0,
        gt=0,
        description="XREAD BLOCK 的上限，只用来让循环醒来查墓碑/发心跳；心跳不依赖它计时。",
    )

    @model_validator(mode="after")
    def _socket_timeout_outlives_block(self) -> "StreamBridgeConfig":
        minimum = self.read_block_seconds + _BLOCK_TIMEOUT_SLACK_SECONDS
        if self.socket_timeout_seconds < minimum:
            raise ValueError(
                "stream_bridge.socket_timeout_seconds must be at least "
                f"read_block_seconds + {_BLOCK_TIMEOUT_SLACK_SECONDS:g} "
                f"({minimum:g}s when read_block_seconds={self.read_block_seconds:g}); "
                "XREAD BLOCK holds the socket until Redis replies, and a shorter "
                "client timeout raises TimeoutError on an idle read"
            )
        return self
