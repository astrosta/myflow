from pydantic import BaseModel, Field


class RunsConfig(BaseModel):
    timeout_seconds: float | None = Field(default=None, gt=0, description="整轮run执行的上限时间")
    shutdown_timeout_seconds: float | None = Field(default=10.0, gt=0, description="排空等worker的时间")
    