import os
from datetime import timedelta
from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func
from app.database import get_db
from app.models import Alert, Device
from app.services.utils import get_current_time_et

router = APIRouter()

# Dynamically locate the templates folder
CURRENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(CURRENT_DIR, "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

@router.get("/", response_class=HTMLResponse)
def read_root(request: Request, db: Session = Depends(get_db)):
    recent_alerts = db.query(Alert).options(joinedload(Alert.device)).order_by(Alert.timestamp.desc()).limit(5).all()

    now = get_current_time_et()
    one_hour_ago = now - timedelta(hours=1)
    twenty_four_hours_ago = now - timedelta(hours=24)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    alerts_last_hour = db.query(Alert).filter(Alert.timestamp >= one_hour_ago).count()
    alerts_last_24h = db.query(Alert).filter(Alert.timestamp >= twenty_four_hours_ago).count()

    ram_count = db.query(Alert).filter(Alert.alert_type.ilike('%ram%'), Alert.timestamp >= twenty_four_hours_ago).count()
    cpu_count = db.query(Alert).filter(Alert.alert_type.ilike('%cpu%'), Alert.timestamp >= twenty_four_hours_ago).count()
    
    if ram_count > cpu_count:
        trend, trend_param = "RAM Spikes", "ram"
    elif cpu_count > ram_count:
        trend, trend_param = "CPU Spikes", "cpu"
    elif ram_count == 0 and cpu_count == 0:
        trend, trend_param = "Stable", ""
    else:
        trend, trend_param = "Even Split", ""

    top_client_query = (
        db.query(Device.client_id, func.count(Alert.alertuid).label('total'))
        .join(Alert).filter(Alert.timestamp >= today_start)
        .group_by(Device.client_id).order_by(func.count(Alert.alertuid).desc()).first()
    )
    top_client = top_client_query.client_id if top_client_query else "None"

    stats = {
        "last_hour": alerts_last_hour,
        "last_24h": alerts_last_24h,
        "trend": trend,
        "trend_param": trend_param,
        "top_client": top_client
    }

    return templates.TemplateResponse("index.html", {"request": request, "recent_alerts": recent_alerts, "stats": stats})

@router.get("/alerts")
def view_alerts(
    request: Request,
    db: Session = Depends(get_db),
    timeframe: str | None = None,
    client: str | None = None,
    trend: str | None = None,
    host_scope: str | None = None,
):
    query = db.query(Alert).join(Device).options(joinedload(Alert.device))
    now = get_current_time_et()
    
    if timeframe == '1h':
        query = query.filter(Alert.timestamp >= now - timedelta(hours=1))
    elif timeframe == '24h':
        query = query.filter(Alert.timestamp >= now - timedelta(hours=24))
        
    if client and client != "None":
        query = query.filter(Device.client_id == client)
        
    if trend == 'ram':
        query = query.filter(Alert.alert_type.ilike('%ram%'), Alert.timestamp >= now - timedelta(hours=24))
    elif trend == 'cpu':
        query = query.filter(Alert.alert_type.ilike('%cpu%'), Alert.timestamp >= now - timedelta(hours=24))

    if host_scope == "citrix":
        query = query.filter(Alert.citrix_host.is_(True))
    elif host_scope == "non_citrix":
        query = query.filter(Alert.citrix_host.is_(False))

    alerts = query.order_by(Alert.timestamp.desc()).limit(100).all()

    return templates.TemplateResponse(
        "alerts.html",
        {"request": request, "alerts": alerts, "host_scope": host_scope or "all"},
    )

@router.get("/report")
async def report_page(request: Request):
    return templates.TemplateResponse("reports.html", {"request": request})

@router.get("/test-alert")
async def test_alert_page(request: Request):
    return templates.TemplateResponse("test-alert.html", {"request": request})


@router.get("/datto-sync", response_class=HTMLResponse)
def datto_sync_viewport(request: Request):
    """Live listing/ingest metrics while Datto sync runs (polls /api/datto/sync-progress)."""
    return templates.TemplateResponse("sync-viewport.html", {"request": request})