import os
from fastapi import APIRouter, BackgroundTasks, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import text, func
from datetime import datetime
from app.database import get_db
from app.models import Client, Device, Alert
from app.schemas import GenerateReportRequest, TestFetchAlertRequest
from app.services.datto import (
    fetch_datto_diagnostics,
    shape_raw_datto_response,
    shape_alert_from_datto,
    get_datto_access_token,
    get_configured_site_uids,
    fetch_site_alerts_for_state_all_pages,
    get_alert_list_max_pages,
    get_ingest_concurrency,
)
from app.services.alert_ingest import sync_alerts_from_datto
from app.services.sync_job import run_sync_in_background, get_last_sync
from app.services.sync_progress import get_snapshot
from app.services.ingest_cutoff import get_ingest_cutoff_et_naive
from app.services.ingest_diagnostic import run_ingest_diagnostic
from app.services.ingest_parse_preview import run_ingest_parse_preview

router = APIRouter()

def _query_alerts_for_report(db: Session, client: str, server: str | None, start_date: datetime, end_date: datetime):
    q = db.query(Alert).join(Device).options(joinedload(Alert.device)).filter(
        Device.client_id == client,
        Alert.timestamp >= start_date,
        Alert.timestamp <= end_date,
    )
    if server:
        q = q.filter(Device.hostname == server)
    return q.order_by(Alert.timestamp.desc()).all()

@router.post("/generate-report")
def generate_report(payload: GenerateReportRequest, db: Session = Depends(get_db)):
    try:
        start_date = datetime.fromisoformat(payload.startDate.replace("Z", "+00:00"))
        end_date = datetime.fromisoformat(payload.endDate.replace("Z", "+00:00"))
    except ValueError:
        return JSONResponse(status_code=400, content={"success": False, "message": "Invalid date format."})
        
    alerts = _query_alerts_for_report(db, payload.client, payload.server, start_date, end_date)
    
    html_rows = ""
    for alert in alerts:
        ts = alert.timestamp.strftime("%Y-%m-%d %H:%M") if alert.timestamp else ""
        client_id = alert.device.client_id if alert.device else "—"
        html_rows += f"<tr><td>{alert.alertuid}</td><td>{client_id}</td><td>{alert.device_hostname}</td><td>{alert.alert_type}</td><td>{alert.alert_category}</td><td>{ts}</td><td>{alert.status}</td></tr>"
        
    html_report = (
        "<div class='report-table'><table class='table table-bordered'><thead><tr>"
        "<th>Alert UID</th><th>Client</th><th>Device</th><th>Alert Type</th><th>Alert Category</th><th>Timestamp</th><th>Status</th>"
        "</tr></thead><tbody>" + html_rows + "</tbody></table></div>"
    )
    return JSONResponse(content={"success": True, "htmlReport": html_report, "count": len(alerts)})

@router.get("/report/csv")
def download_report_csv(
    db: Session = Depends(get_db),
    client: str = Query(...),
    startDate: str = Query(...),
    endDate: str = Query(...),
    server: str | None = Query(None),
):
    try:
        start_date = datetime.fromisoformat(startDate + "T00:00:00")
        end_date = datetime.fromisoformat(endDate + "T23:59:59")
    except ValueError:
        return JSONResponse(status_code=400, content={"detail": "Invalid date format."})
        
    alerts = _query_alerts_for_report(db, client, server, start_date, end_date)

    def csv_stream():
        yield "Alert UID,Client,Device,Alert Type,Alert Category,Timestamp,Status\n"
        for alert in alerts:
            client_id = alert.device.client_id if alert.device else ""
            ts = alert.timestamp.strftime("%Y-%m-%d %H:%M") if alert.timestamp else ""
            yield f'"{alert.alertuid}","{client_id}","{alert.device_hostname}","{alert.alert_type}","{alert.alert_category}","{ts}","{alert.status}"\n'

    return StreamingResponse(
        csv_stream(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rmm_alerts_report.csv"},
    )

@router.get("/api/clients")
def list_clients(db: Session = Depends(get_db)):
    clients = db.query(Client).order_by(Client.id).all()
    return [{"id": c.id, "name": c.name or c.id} for c in clients]

@router.get("/api/devices")
def list_devices(db: Session = Depends(get_db), client_id: str | None = Query(None)):
    q = db.query(Device).order_by(Device.hostname)
    if client_id:
        q = q.filter(Device.client_id == client_id)
    return [{"hostname": d.hostname, "client_id": d.client_id} for d in q.all()]

@router.get("/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "database": "disconnected"})
    return {"status": "healthy", "database": "connected"}

@router.get("/api/datto/status")
async def datto_api_status():
    """Lightweight check that API credentials can obtain a token and site UIDs are configured."""
    token = await get_datto_access_token()
    sites = get_configured_site_uids()
    cutoff = get_ingest_cutoff_et_naive()
    return {
        "configured": bool(os.getenv("DATTO_API_KEY")),
        "token_ok": bool(token),
        "site_uids_configured": len(sites),
        "site_uids": sites,
        "alert_ingest_cutoff_et": cutoff.isoformat(sep=" ", timespec="seconds"),
        "alert_list_max_pages_per_state": get_alert_list_max_pages(),
        "ingest_concurrency": get_ingest_concurrency(),
    }


@router.get("/api/datto/alerts-count")
def datto_alerts_count(db: Session = Depends(get_db)):
    """Total Alert rows in the connected database (sanity check after sync)."""
    n = db.query(func.count(Alert.alertuid)).scalar() or 0
    return {"alerts_total": n}


@router.get("/api/datto/ingest-diagnostic")
async def datto_ingest_diagnostic():
    """
    Read-only: token + first site first page of open alerts + one detail sample.
    Use when POST /api/datto/sync-alerts returns ingested: 0 — shows HTTP errors, JSON shape, cutoff, and CPU/MEMORY filter.
    """
    return await run_ingest_diagnostic()


@router.get("/api/datto/ingest-parse-preview")
async def datto_ingest_parse_preview(
    db: Session = Depends(get_db),
    site_uid: str | None = Query(
        None,
        description="Site UID to inspect; defaults to the first value in DATTO_SITE_UIDS.",
    ),
    rows_per_state: int = Query(
        20,
        ge=1,
        le=80,
        description="How many rows from page 0 of each list (open/resolved) to include in sample_rows.",
    ),
    detail_samples: int = Query(
        12,
        ge=0,
        le=40,
        description="How many merged-queue UIDs (page 0 only) to detail-fetch and classify like sync would.",
    ),
):
    """
    Read-only visibility into **what the sync parser sees** on Datto list page 0 per state (same state order
    as sync), plus optional per-UID explanations after a detail GET (cutoff, CPU/MEMORY filter, DB duplicate).

    Does not write to the database. Full sync still paginates all pages; this endpoint is intentionally fast.
    """
    return await run_ingest_parse_preview(db, site_uid, rows_per_state, detail_samples)


@router.get("/api/datto/alerts-preview")
async def datto_alerts_preview():
    """
    Per-site open/resolved alert list counts (paginated up to DATTO_ALERT_LIST_MAX_PAGES per state).
    Does not write to the DB.
    """
    sites = get_configured_site_uids()
    if not sites:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": "no_sites_configured", "sites": {}},
        )
    result: dict[str, dict] = {}
    mp = get_alert_list_max_pages()
    for site_uid in sites:
        open_r = await fetch_site_alerts_for_state_all_pages(site_uid, "open", max_pages=mp)
        resolved_r = await fetch_site_alerts_for_state_all_pages(site_uid, "resolved", max_pages=mp)
        if open_r is None or resolved_r is None:
            result[site_uid] = {
                "ok": False,
                "error": "list_fetch_failed",
                "open_count": None,
                "resolved_count": None,
            }
        else:
            result[site_uid] = {
                "ok": True,
                "open_count": len(open_r.items),
                "resolved_count": len(resolved_r.items),
                "open_list_truncated": open_r.truncated_by_max_pages,
                "resolved_list_truncated": resolved_r.truncated_by_max_pages,
                "max_pages_per_state": mp,
            }
    return {"ok": True, "sites": result}


@router.get("/api/datto/last-sync")
def datto_last_sync():
    """Result of the most recent background sync (see POST /api/datto/sync-alerts)."""
    return get_last_sync()


@router.get("/api/datto/sync-progress")
def datto_sync_progress():
    """
    Job status from the background worker plus **live** listing/ingest counters during an active sync.
    Poll every 1–2s from the Datto sync viewport page.
    """
    return {**get_last_sync(), "live": get_snapshot()}


@router.post("/api/datto/sync-alerts")
async def datto_sync_alerts(
    background_tasks: BackgroundTasks,
    trace_limit: int = Query(0, ge=0, le=50),
    inline: bool = Query(
        False,
        description="If true, run the full sync in the request (may time out behind HTTPS proxies).",
    ),
):
    """
    Poll Datto for each configured site: open + resolved alert lists (paginated),
    then detail-fetch and ingest new CPU/RAM performance alerts (rate-limited).

    By default the sync runs **after** the HTTP response returns (HTTP 202) so reverse proxies
    do not cut the connection. Poll GET /api/datto/last-sync until `sync_running` is false.

    Use `?inline=1` only for small tests or when calling the app directly without a short read timeout.

    Use `?trace_limit=20` to append per-UID outcomes for debugging (inline mode only).
    """
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


@router.post("/api/test-fetch-alert")
async def test_fetch_alert(payload: TestFetchAlertRequest):
    raw = await fetch_datto_diagnostics(payload.alertuid)
    shaped = shape_raw_datto_response(raw)
    return {"shaped": shaped, "raw_from_datto": raw if raw else None}

@router.get("/api/alerts/{alertuid}")
def get_alert(alertuid: str, db: Session = Depends(get_db)):
    alert = db.query(Alert).options(joinedload(Alert.device)).filter(Alert.alertuid == alertuid).first()
    if not alert:
        return JSONResponse(status_code=404, content={"detail": "Alert not found."})
    return shape_alert_from_datto(
        alert.diagnostic_data, alert.alertuid, alert.device_hostname,
        alert.device.client_id if alert.device else None,
        alert.alert_type, alert.alert_category, alert.status, alert.timestamp
    )

@router.patch("/api/alerts/{alertuid}/status")
def update_alert_status(alertuid: str, status: str = Query(..., regex="^(Open|Resolved)$"), db: Session = Depends(get_db)):
    alert = db.query(Alert).filter(Alert.alertuid == alertuid).first()
    if not alert:
        return JSONResponse(status_code=404, content={"detail": "Alert not found."})
    alert.status = status
    db.commit()
    return {"alertuid": alertuid, "status": status}