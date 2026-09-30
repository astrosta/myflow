from pydantic import BaseModel, Field
from typing import Literal

class StreamBridgeConfig(BaseModel):
    backend: Literal["memory", "redis"] = "memory"
    queue_maxsize: int = Field(default=256, gt=0)                    # 每 run 保留的数据事件数
    heartbeat_interval_seconds: float = Field(default=15.0, ge=0)    # 0 = 关掉心跳（只给嵌入式用）