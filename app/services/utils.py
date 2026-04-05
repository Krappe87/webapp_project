import re
from datetime import datetime
from zoneinfo import ZoneInfo

def get_current_time_et():
    """Returns the current Eastern Time as a clean, database-ready datetime object."""
    return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)

def extract_client_id(hostname: str) -> str:
    """Parses the hostname to extract the 3 or 4 letter/number client identifier code."""
    hostname = hostname.upper()
    
    if hostname.startswith("CET-FILE-"):
        return "UNKNOWN"
        
    match_front = re.match(r'^([A-Z0-9]{3,4})-(APP|SQL)', hostname)
    if match_front:
        return match_front.group(1)
        
    match_end = re.search(r'\d{2}-?([A-Z0-9]{3,4})$', hostname)
    if match_end:
        return match_end.group(1)
        
    return "UNKNOWN"