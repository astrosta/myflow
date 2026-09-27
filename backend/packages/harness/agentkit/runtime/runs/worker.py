"""
run_agent
"""
import asyncio
from typing import Callable, Any
from agentkit.runtime.streams.bridge import StreamBridge
from agentkit.runtime.runs.manager import RunManager, RunRecord
from agentkit.runtime.context import RunContext
from agentkit.runtime.runs.schemas import RunStatus, RunStartOutcome


STREAM_RETENTION_SECONDS = 60.0      # END 之后桥缓冲再留多久，给晚到的 /join 续传
AgentFactory = Callable[..., Any]

class RunAborted(Exception):
    def __init__(self, action: str) -> None:
        super().__init__(action)
        self.action = action


async def run_agent(
    bridge: StreamBridge,
    run_manager: RunManager,
    record: RunRecord,
    ctx: RunContext,
) -> None:
    """一条run从启动到end的全过程，不返回值，终态写进store，事件写进bridge"""
    run_id, thread_id = record.run_id, record.thread_id
    terminal, error, error_type = RunStatus.interrupted, None, None #try_start 拒绝、提前return时的终态
    rollback: RollbackPoint | None = None 
    usage = UsageCollector(run_manager, run_id)
    deadline: asyncio.Timeout | None = None
    timeout_s: float | None = None

    try:
        await run_manager.wait_for_prior_finalizing(thread_id, run_id, abort_event=record.abort_event)

        if await run_manager.try_start(run_id) is RunStartOutcome.cancelled:
            return

        await ctx.thread_store.set_status(thread_id, "running")
        rollback = await capture_rollback_point(ctx, thread_id)
        