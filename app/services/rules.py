from sqlalchemy.orm import Session
from sqlalchemy import func, not_
from datetime import timedelta
from app.models import Alert, Device
from app.services.utils import get_current_time_et

def run_rule_engine(client_id: str, db: Session):
    """Evaluates real-time rules immediately after a single alert is ingested."""
    one_week_ago = get_current_time_et() - timedelta(days=7)
    weekly_alerts = db.query(Alert).join(Device).filter(
        Device.client_id == client_id, Alert.timestamp >= one_week_ago
    ).count()
    
    if weekly_alerts >= 5:
        print(f"TRIGGER: Client {client_id} hit {weekly_alerts} alerts in the last 7 days!")

def get_current_week_boundaries():
    """Calculates the exact start (Sunday) and end (Saturday) of the current week."""
    now = get_current_time_et()
    
    # Shifts it so Sunday subtracts 0 days, Monday subtracts 1, etc.
    days_to_subtract = (now.weekday() + 1) % 7
    
    start_of_week = (now - timedelta(days=days_to_subtract)).replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_week = start_of_week + timedelta(days=6, hours=23, minutes=59, seconds=59)
    
    return start_of_week, end_of_week

def calculate_weekly_totals(db: Session):
    """
    Scans the DB for the current week and returns the totals for SharePoint.
    Explicitly ignores APP, SQL, and FILE servers.
    """
    start_date, end_date = get_current_week_boundaries()
    week_label = f"Week of {start_date.strftime('%m-%d-%Y')}"

    # Query the DB, explicitly filtering out infrastructure servers
    results = (
        db.query(Device.client_id, func.count(Alert.alertuid).label('total_alerts'))
        .join(Alert)
        .filter(
            Alert.timestamp >= start_date, 
            Alert.timestamp <= end_date,
            not_(Device.hostname.ilike('%-APP-%')),
            not_(Device.hostname.ilike('%-SQL-%')),
            not_(Device.hostname.ilike('CET-FILE-%')),
            Device.client_id != 'UNKNOWN' # Extra safety catch
        )
        .group_by(Device.client_id)
        .all()
    )
    
    # Convert to a clean dictionary: {'ABC': 12, 'XYZ': 3}
    client_totals = {row.client_id: row.total_alerts for row in results if row.client_id}
    
    return week_label, client_totals