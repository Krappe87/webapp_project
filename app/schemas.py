from pydantic import BaseModel
from typing import Optional

class RMMWebhookPayload(BaseModel):
    device_hostname: str
    alert_type: str
    alert_category: str
    alertuid: str

class GenerateReportRequest(BaseModel):
    client: str
    server: Optional[str] = None
    startDate: str
    endDate: str

class TestFetchAlertRequest(BaseModel):
    alertuid: str