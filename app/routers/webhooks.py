import os
import secrets
from fastapi import APIRouter, Request, BackgroundTasks, HTTPException
from sqlalchemy.exc import IntegrityError
from app.database import SessionLocal
from app.models import Client, Device, Alert
from app.schemas import RMMWebhookPayload
from app.services.datto import fetch_datto_diagnostics, reduce_datto_payload_for_storage
from app.services.rules import run_rule_engine
from app.services.utils import extract_client_id

router = APIRouter()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

async def process_alert_background(alertuid: str, hostname: str, alert_type: str, alert_category: str):
    db = SessionLocal()
    client_id = extract_client_id(hostname)
    
    try:
        client = db.query(Client).filter(Client.id == client_id).first()
        if not client:
            client = Client(id=client_id, name=f"Client {client_id}")
            db.add(client)
            
        device = db.query(Device).filter(Device.hostname == hostname).first()
        if not device:
            device = Device(hostname=hostname, client_id=client_id)
            db.add(device)
            
        db.commit()

        raw_diagnostics = await fetch_datto_diagnostics(alertuid)
        diagnostic_data = reduce_datto_payload_for_storage(raw_diagnostics)

        existing = db.query(Alert).filter(Alert.alertuid == alertuid).first()
        if existing:
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
            return

        run_rule_engine(client_id, db)

    finally:
        db.close()

@router.post("/webhook")
async def receive_rmm_alert(request: Request, payload: RMMWebhookPayload, background_tasks: BackgroundTasks):
    if WEBHOOK_SECRET:
        incoming_secret = request.headers.get("X-Webhook-Secret")
        if not incoming_secret:
            raise HTTPException(status_code=401, detail="Missing authentication header")
        if not secrets.compare_digest(incoming_secret, WEBHOOK_SECRET):
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    background_tasks.add_task(
        process_alert_background, 
        payload.alertuid, 
        payload.device_hostname,
        payload.alert_type,
        payload.alert_category
    )
    return {"status": "success"}