"""
Ingestion entrypoints (no inbound webhooks).

Alerts are polled from Datto per site (DATTO_SITE_UIDS): open + resolved lists,
paginated, rate-limited; only alerts with alertContext.type CPU or MEMORY are stored.
"""

from fastapi import APIRouter, BackgroundTasks, Query
from fastapi.responses import JSONResponse

from app.services.alert_ingest import sync_alerts_from_datto
from app.services.sync_job import run_sync_in_background

router = APIRouter()


@router.post("/sync/alerts-from-datto")
async def sync_alerts_from_datto_short(
    background_tasks: BackgroundTasks,
    trace_limit: int = Query(0, ge=0, le=50),
    inline: bool = Query(
        False,
        description="If true, run the full sync in the request (may time out behind HTTPS proxies).",
    ),
):
    """Same as POST /api/datto/sync-alerts (optional ?trace_limit=, ?inline=)."""
    if inline:
        return await sync_alerts_from_datto(trace_limit=trace_limit)
    background_tasks.add_task(run_sync_in_background, trace_limit)
    return JSONResponse(
        status_code=202,
        content={
            "accepted": True,
            "sync_mode": "background",
            "trace_limit": trace_limit,
            "message": "Sync started. Poll GET /api/datto/last-sync until sync_running is false.",
        },
    )
