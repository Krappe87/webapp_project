"""Background Datto sync job (avoids HTTP gateway timeouts on large syncs)."""

import asyncio
import time
import traceback

from app.services.alert_ingest import sync_alerts_from_datto

_state_lock: asyncio.Lock | None = None
_state_lock_loop_id: int | None = None
_running = False
_last_result: dict | None = None
_last_finished_at: float | None = None


def _sync_state_lock() -> asyncio.Lock:
    """Bind the lock to the current running event loop (not at import time)."""
    global _state_lock, _state_lock_loop_id
    loop = asyncio.get_running_loop()
    lid = id(loop)
    if _state_lock is None or _state_lock_loop_id != lid:
        _state_lock = asyncio.Lock()
        _state_lock_loop_id = lid
    return _state_lock


def get_last_sync() -> dict:
    """Latest sync outcome (or running state)."""
    return {
        "sync_running": _running,
        "last_finished_at_epoch": _last_finished_at,
        "last_result": _last_result,
    }


async def run_sync_in_background(trace_limit: int = 0) -> None:
    global _running, _last_result, _last_finished_at

    # Hold the lock for the whole run so only one sync executes at a time in this process.
    # Waiters block here until the active run finishes; they then see _running False and may
    # start another full sync—avoid overlapping cron/POST triggers if back-to-back runs are unwanted.
    async with _sync_state_lock():
        if _running:
            return
        _running = True
        try:
            _last_result = await sync_alerts_from_datto(trace_limit=trace_limit)
        except Exception as exc:
            _last_result = {
                "ok": False,
                "error": str(exc),
                "error_type": type(exc).__name__,
                "traceback": traceback.format_exc()[:4000],
            }
        finally:
            _last_finished_at = time.time()
            _running = False
