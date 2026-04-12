"""Background Datto sync job (avoids HTTP gateway timeouts on large syncs)."""

import asyncio
import time
import traceback

from app.services.alert_ingest import sync_alerts_from_datto

_state_lock = asyncio.Lock()
_running = False
_last_result: dict | None = None
_last_finished_at: float | None = None


def get_last_sync() -> dict:
    """Latest sync outcome (or running state)."""
    return {
        "sync_running": _running,
        "last_finished_at_epoch": _last_finished_at,
        "last_result": _last_result,
    }


async def run_sync_in_background(trace_limit: int = 0) -> None:
    global _running, _last_result, _last_finished_at

    async with _state_lock:
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
        async with _state_lock:
            _running = False
