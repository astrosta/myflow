"""Redis Streams transport for the capture queue."""

from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis

from agentkit.agents.memory.openviking.contracts import CapturedMessage
from agentkit.agents.memory.openviking.stream import RedisCaptureStream


@pytest.mark.asyncio
async def test_redis_stream_publish_read_ack() -> None:
    redis = FakeRedis(decode_responses=True)
    stream = RedisCaptureStream(
        redis,
        key_prefix="test:capture",
        group="workers",
        consumer="worker-1",
        maxlen=100,
    )
    captured = CapturedMessage(
        user_id="u1",
        thread_id="t1",
        turn_id="r1",
        message_id="m1",
        session_epoch=1,
        session_incarnation="i1",
        sequence=0,
        kind="human",
        role="user",
        message={"type": "human", "content": "hi"},
        stream_shard=3,
    )

    result = await stream.publish(captured)
    assert result.stream_id
    items = await stream.read(3, block_ms=1)
    assert len(items) == 1
    assert items[0].message_id == "m1"
    assert items[0].stream_id == result.stream_id

    await stream.ack(result.stream_id or "", shard=3)
    assert await stream.pending_count(3) == 0
