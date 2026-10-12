"""Windows-safe asyncio loop factory for uvicorn + psycopg async.

Uvicorn's built-in ``asyncio`` / ``auto`` factories hardcode
``ProactorEventLoop`` on win32. Custom ``--loop module:attr`` is different:
uvicorn imports ``attr`` and uses it **directly** as
``Callable[[], AbstractEventLoop]`` (usually the loop class). It does **not**
call a ``(use_subprocess) -> loop_class`` factory the way built-ins do.

Start the gateway with::

    uvicorn ... --loop backend.app.gateway.selector_loop:selector_loop_factory
"""

from __future__ import annotations

import asyncio

# Zero-arg callable: asyncio.Runner does ``loop = loop_factory()``.
selector_loop_factory = asyncio.SelectorEventLoop
