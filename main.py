import os
import re
from datetime import datetime, timedelta
from fastapi import FastAPI, BackgroundTasks, Request, Depends, Query
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, String, DateTime, ForeignKey, Integer, func, distinct, text
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session, joinedload
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
import httpx

# --- DATABASE SETUP ---
# Grabs the database URL from Docker Compose, defaults to localhost if missing
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://rmmuser:rmmpassword@localhost:5432/rmmdb")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# --- MODELS ---
class Client(Base):
    __tablename__ = 'clients'
    id = Column(String, primary_key=True) 
    name = Column(String, nullable=True)
    total_servers = Column(Integer, default=0)
    devices = relationship("Device", back_populates="client")

class Device(Base):
    __tablename__ = 'devices'
    hostname = Column(String, primary_key=True)
    client_id = Column(String, ForeignKey('clients.id'))
    client = relationship("Client", back_populates="devices")
    alerts = relationship("Alert", back_populates="device")

class Alert(Base):
    __tablename__ = 'alerts'
    alertuid = Column(String, primary_key=True)
    device_hostname = Column(String, ForeignKey('devices.hostname'))
    alert_type = Column(String)
    alert_category = Column(String)
    diagnostic_data = Column(JSONB) 
    timestamp = Column(DateTime, default=datetime.utcnow)
    status = Column(String, default='Open')
    device = relationship("Device", back_populates="alerts")

# Create tables in the database if they don't exist
Base.metadata.create_all(bind=engine)

# --- FASTAPI SETUP ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Dependency to get the database session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- DATTO API CONFIG ---
DATTO_API_URL = os.getenv("DATTO_API_URL", "https://concord-api.centrastage.net")
API_KEY = os.getenv("DATTO_API_KEY", "")
API_SECRET = os.getenv("DATTO_API_SECRET", "")

# --- HELPER FUNCTIONS ---
def extract_client_id(hostname: str):
    match = re.search(r'\d{2}([A-Za-z]{3,4})$', hostname)
    return match.group(1).upper() if match else "UNKNOWN"

async def get_datto_access_token():
    if not API_KEY or not API_SECRET:
        return None
    token_url = f"{DATTO_API_URL}/auth/oauth/token"
    auth = ("public-client", "public")
    data = {"grant_type": "password", "username": API_KEY, "password": API_SECRET}
    async with httpx.AsyncClient() as client:
        response = await client.post(token_url, data=data, auth=auth)
        if response.status_code == 200:
            return response.json().get("access_token")
        return None


async def fetch_datto_diagnostics(alertuid: str) -> dict:
    """Fetch diagnostic data for an alert from the Datto RMM API. Returns {} on failure."""
    token = await get_datto_access_token()
    if not token:
        return {}
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    endpoint = f"{DATTO_API_URL}/api/v2/alert/{alertuid}"
    async with httpx.AsyncClient() as client:
        response = await client.get(endpoint, headers=headers)
        if response.status_code == 200:
            return response.json()
        return {}

def run_rule_engine(client_id: str, db: Session):
    one_week_ago = datetime.utcnow() - timedelta(days=7)
    weekly_alerts = db.query(Alert).join(Device).filter(
        Device.client_id == client_id, Alert.timestamp >= one_week_ago
    ).count()
    
    if weekly_alerts >= 5:
        print(f"TRIGGER: Client {client_id} hit {weekly_alerts} alerts this week!")

# --- BACKGROUND WORKER ---
async def process_alert_background(alertuid: str, hostname: str, alert_type: str, alert_category: str):
    db = SessionLocal()
    client_id = extract_client_id(hostname)
    
    try:
        # 1. Ensure Client Exists
        client = db.query(Client).filter(Client.id == client_id).first()
        if not client:
            client = Client(id=client_id, name=f"Client {client_id}")
            db.add(client)
            
        # 2. Ensure Device Exists
        device = db.query(Device).filter(Device.hostname == hostname).first()
        if not device:
            device = Device(hostname=hostname, client_id=client_id)
            db.add(device)
            
        db.commit()

        # 3. Fetch Datto Diagnostics and reduce to stored shape
        raw_diagnostics = await fetch_datto_diagnostics(alertuid)
        diagnostic_data = _reduce_datto_payload_for_storage(raw_diagnostics)

        # 4. Save Alert (skip if duplicate webhook)
        existing = db.query(Alert).filter(Alert.alertuid == alertuid).first()
        if existing:
            db.close()
            return
        new_alert = Alert(
            alertuid=alertuid,
            device_hostname=hostname,
            alert_type=alert_type,
            alert_category=alert_category,
            diagnostic_data=diagnostic_data
        )
        db.add(new_alert)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            db.close()
            return

        # 5. Run Rules
        run_rule_engine(client_id, db)

    finally:
        db.close()

# --- ROUTES ---
class RMMWebhookPayload(BaseModel):
    device_hostname: str
    alert_type: str
    alert_category: str
    alertuid: str

@app.post("/webhook")
async def receive_rmm_alert(payload: RMMWebhookPayload, background_tasks: BackgroundTasks):
    background_tasks.add_task(
        process_alert_background, 
        payload.alertuid, 
        payload.device_hostname,
        payload.alert_type,
        payload.alert_category
    )
    return {"status": "success"}

@app.get("/")
async def read_root():
    with open("templates/index.html", "r") as f:
        content = f.read()
    return HTMLResponse(content=content)

@app.get("/dashboard")
def view_dashboard(request: Request, db: Session = Depends(get_db)):
    alerts = (
        db.query(Alert)
        .options(joinedload(Alert.device))
        .order_by(Alert.timestamp.desc())
        .limit(100)
        .all()
    )
    return templates.TemplateResponse("dashboard.html", {"request": request, "alerts": alerts})

@app.get("/report")
async def report_page(request: Request):
    return templates.TemplateResponse("reports.html", {"request": request})

# --- Report request/response models ---
class GenerateReportRequest(BaseModel):
    client: str
    server: str | None = None  # optional: filter by device hostname
    startDate: str
    endDate: str

def _query_alerts_for_report(db: Session, client: str, server: str | None, start_date: datetime, end_date: datetime):
    q = (
        db.query(Alert)
        .join(Device)
        .options(joinedload(Alert.device))
        .filter(
            Device.client_id == client,
            Alert.timestamp >= start_date,
            Alert.timestamp <= end_date,
        )
    )
    if server:
        q = q.filter(Device.hostname == server)
    return q.order_by(Alert.timestamp.desc()).all()

@app.post("/generate-report")
def generate_report(payload: GenerateReportRequest, db: Session = Depends(get_db)):
    try:
        start_date = datetime.fromisoformat(payload.startDate.replace("Z", "+00:00"))
        end_date = datetime.fromisoformat(payload.endDate.replace("Z", "+00:00"))
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Invalid date format. Use YYYY-MM-DD."},
        )
    if start_date > end_date:
        return JSONResponse(
            status_code=400,
            content={"success": False, "message": "Start date must be before end date."},
        )
    alerts = _query_alerts_for_report(
        db, payload.client, payload.server, start_date, end_date
    )
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

@app.get("/report/csv")
def download_report_csv(
    db: Session = Depends(get_db),
    client: str = Query(..., description="Client ID"),
    startDate: str = Query(..., description="Start date YYYY-MM-DD"),
    endDate: str = Query(..., description="End date YYYY-MM-DD"),
    server: str | None = Query(None, description="Optional device hostname"),
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
            line = f'"{alert.alertuid}","{client_id}","{alert.device_hostname}","{alert.alert_type}","{alert.alert_category}","{ts}","{alert.status}"\n'
            yield line

    return StreamingResponse(
        csv_stream(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=rmm_alerts_report.csv"},
    )

# --- API for report form dropdowns ---
@app.get("/api/clients")
def list_clients(db: Session = Depends(get_db)):
    clients = db.query(Client).order_by(Client.id).all()
    return [{"id": c.id, "name": c.name or c.id} for c in clients]

@app.get("/api/devices")
def list_devices(db: Session = Depends(get_db), client_id: str | None = Query(None)):
    q = db.query(Device).order_by(Device.hostname)
    if client_id:
        q = q.filter(Device.client_id == client_id)
    devices = q.all()
    return [{"hostname": d.hostname, "client_id": d.client_id} for d in devices]

# --- Health & alert management ---
@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unhealthy", "database": "disconnected"})
    return {"status": "healthy", "database": "connected"}

# --- Datto payload: reduce to what we store (used at ingest and for display) ---
# We intentionally omit: muted, alertMonitorInfo, responseActions, autoresolveMins
# (not needed for triage, reporting, or client device alerts).
# We keep: priority (triage/sorting), resolvedBy/resolvedOn (accountability & SLA), alertContext (type/percentage etc.).

def _reduce_datto_payload_for_storage(raw: dict | None) -> dict:
    """Reduce raw Datto API response to the subset we store in diagnostic_data (and use everywhere)."""
    if not raw:
        return {}
    source = raw.get("alertSourceInfo") or {}
    context = raw.get("alertContext") or {}
    return {
        "alertUid": raw.get("alertUid"),
        "priority": raw.get("priority"),
        "diagnostics": raw.get("diagnostics"),
        "resolved": raw.get("resolved"),
        "resolvedBy": raw.get("resolvedBy"),
        "resolvedOn": raw.get("resolvedOn"),
        "ticketNumber": raw.get("ticketNumber"),
        "timestamp": raw.get("timestamp"),
        "alertSourceInfo": raw.get("alertSourceInfo"),
        "alertContext": context if context else None,
        "device_hostname": source.get("deviceName"),
        "alert_type": context.get("type"),
        "alert_percent": context.get("percentage"),
    }


def _shape_alert_from_datto(diagnostic_data: dict | None, alert_uid: str, device_hostname: str, client_id: str | None, alert_type: str | None, alert_category: str | None, status: str, timestamp: datetime | None) -> dict:
    """Build get_alert response from DB alert and stored (reduced) Datto diagnostic JSON."""
    d = diagnostic_data or {}
    return {
        "alertUid": d.get("alertUid") or alert_uid,
        "priority": d.get("priority"),
        "diagnostics": d.get("diagnostics"),
        "resolved": d.get("resolved"),
        "resolvedBy": d.get("resolvedBy"),
        "resolvedOn": d.get("resolvedOn"),
        "ticketNumber": d.get("ticketNumber"),
        "timestamp": d.get("timestamp") or (timestamp.isoformat() if timestamp else None),
        "alertSourceInfo": d.get("alertSourceInfo"),
        "alertContext": d.get("alertContext"),
        "device_hostname": device_hostname,
        "client_id": client_id,
        "alert_type": d.get("alert_type") or alert_type,
        "alert_percent": d.get("alert_percent"),
        "alert_category": alert_category,
        "status": status,
    }


def _shape_raw_datto_response(d: dict | None) -> dict:
    """Shape raw Datto API JSON into our display/ingest format (no DB). For test-fetch only."""
    reduced = _reduce_datto_payload_for_storage(d)
    if not reduced:
        return {}
    # Add display-only fields that we don't store
    reduced["status"] = "Resolved" if reduced.get("resolved") else "Open"
    reduced["client_id"] = None
    reduced["alert_category"] = None
    return reduced


class TestFetchAlertRequest(BaseModel):
    alertuid: str


@app.post("/api/test-fetch-alert")
async def test_fetch_alert(payload: TestFetchAlertRequest):
    """Fetch alert from Datto API and return shaped response (no DB). For debugging/testing."""
    raw = await fetch_datto_diagnostics(payload.alertuid)
    shaped = _shape_raw_datto_response(raw)
    return {"shaped": shaped, "raw_from_datto": raw if raw else None}


@app.get("/test-alert")
async def test_alert_page(request: Request):
    """Debug page: manually fetch an alert by UID and view shaped output (no DB ingest)."""
    return templates.TemplateResponse("test-alert.html", {"request": request})


@app.get("/api/alerts/{alertuid}")
def get_alert(alertuid: str, db: Session = Depends(get_db)):
    """Get a single alert by UID. Response uses Datto API shape; key fields: alertUid, diagnostics, alertSourceInfo (e.g. deviceName)."""
    alert = (
        db.query(Alert)
        .options(joinedload(Alert.device))
        .filter(Alert.alertuid == alertuid)
        .first()
    )
    if not alert:
        return JSONResponse(status_code=404, content={"detail": "Alert not found."})
    return _shape_alert_from_datto(
        alert.diagnostic_data,
        alert.alertuid,
        alert.device_hostname,
        alert.device.client_id if alert.device else None,
        alert.alert_type,
        alert.alert_category,
        alert.status,
        alert.timestamp,
    )


@app.patch("/api/alerts/{alertuid}/status")
def update_alert_status(alertuid: str, status: str = Query(..., regex="^(Open|Resolved)$"), db: Session = Depends(get_db)):
    alert = db.query(Alert).filter(Alert.alertuid == alertuid).first()
    if not alert:
        return JSONResponse(status_code=404, content={"detail": "Alert not found."})
    alert.status = status
    db.commit()
    return {"alertuid": alertuid, "status": status}