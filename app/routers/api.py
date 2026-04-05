from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import text
from datetime import datetime
from app.database import get_db
from app.models import Client, Device, Alert
from app.schemas import GenerateReportRequest, TestFetchAlertRequest
from app.services.datto import fetch_datto_diagnostics, shape_raw_datto_response, shape_alert_from_datto

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