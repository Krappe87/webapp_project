import os
import httpx
from datetime import datetime

DATTO_API_URL = os.getenv("DATTO_API_URL", "https://concord-api.centrastage.net")
API_KEY = os.getenv("DATTO_API_KEY", "")
API_SECRET = os.getenv("DATTO_API_SECRET", "")

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
    """Fetch diagnostic data for an alert from the Datto RMM API."""
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

def reduce_datto_payload_for_storage(raw: dict | None) -> dict:
    """Reduce raw Datto API response to the subset we store."""
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

def shape_alert_from_datto(diagnostic_data: dict | None, alert_uid: str, device_hostname: str, client_id: str | None, alert_type: str | None, alert_category: str | None, status: str, timestamp: datetime | None) -> dict:
    """Build API response from DB alert and stored Datto diagnostic JSON."""
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

def shape_raw_datto_response(d: dict | None) -> dict:
    """Shape raw Datto API JSON into our display format for testing."""
    reduced = reduce_datto_payload_for_storage(d)
    if not reduced:
        return {}
    reduced["status"] = "Resolved" if reduced.get("resolved") else "Open"
    reduced["client_id"] = None
    reduced["alert_category"] = None
    return reduced