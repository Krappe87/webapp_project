from pydantic import BaseModel
from typing import Optional

class GenerateReportRequest(BaseModel):
    client: str
    server: Optional[str] = None
    startDate: str
    endDate: str

class TestFetchAlertRequest(BaseModel):
    alertuid: str