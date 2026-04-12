"""Throttle Datto API calls to stay under account rate limits (default 600/min)."""

import asyncio
import os
import time

# Datto documents ~600 requests per 60 seconds; stay slightly under.
_requests_per_minute = int(os.getenv("DATTO_MAX_REQUESTS_PER_MINUTE", "550"))
_min_interval = 60.0 / max(_requests_per_minute, 1)

_lock: asyncio.Lock | None = None
_lock_loop_id: int | None = None
_last_request_monotonic = 0.0


def _rate_limit_lock() -> asyncio.Lock:
    """Bind the lock to the current running event loop (not at import time)."""
    global _lock, _lock_loop_id
    loop = asyncio.get_running_loop()
    lid = id(loop)
    if _lock is None or _lock_loop_id != lid:
        _lock = asyncio.Lock()
        _lock_loop_id = lid
    return _lock


async def wait_for_datto_rate_limit() -> None:
    """Block until the next Datto API call is allowed."""
    global _last_request_monotonic
    async with _rate_limit_lock():
        now = time.monotonic()
        wait = _min_interval - (now - _last_request_monotonic)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_monotonic = time.monotonic()
