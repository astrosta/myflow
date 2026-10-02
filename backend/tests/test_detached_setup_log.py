"""A cancelled thread-metadata setup must not drop its exception on the floor."""

import asyncio
import logging

from agentkit.runtime.runs.worker import _log_detached_setup_result


def _run(scenario) -> None:
    asyncio.run(scenario())


def test_failed_setup_is_logged(caplog) -> None:
    async def scenario() -> None:
        async def boom() -> None:
            raise RuntimeError("disk")

        task = asyncio.create_task(boom())
        await asyncio.sleep(0)
        with caplog.at_level(logging.WARNING):
            _log_detached_setup_result(task)
        assert task.exception() is not None

    _run(scenario)
    assert "Detached thread metadata setup failed" in caplog.text
    assert "disk" in caplog.text


def test_cancelled_setup_is_quiet(caplog) -> None:
    async def scenario() -> None:
        async def hang() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(hang())
        task.cancel()
        await asyncio.sleep(0)
        with caplog.at_level(logging.WARNING):
            _log_detached_setup_result(task)

    _run(scenario)
    assert caplog.text == ""
