"""Hard cutoff for Datto alert ingestion (ignore alerts before a configured date)."""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# Inclusive: alerts at or after this instant (America/New_York) are eligible.
# Default: start of 2026-01-01 Eastern.
ALERT_INGEST_SINCE = os.getenv("ALERT_INGEST_SINCE", "2026-01-01").strip()
_ET = ZoneInfo("America/New_York")


def get_ingest_cutoff_et_naive() -> datetime:
    """
    Wall-clock start of the cutoff date in Eastern Time, stored naive (matches app DB datetimes).
    Alerts strictly before this moment are not ingested.
    """
    s = ALERT_INGEST_SINCE
    if "T" in s:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_ET)
        else:
            dt = dt.astimezone(_ET)
        return dt.replace(tzinfo=None)
    parts = s.split("-")
    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    aware = datetime(y, m, d, 0, 0, 0, tzinfo=_ET)
    return aware.replace(tzinfo=None)


def parse_datto_timestamp_to_et_naive(value) -> datetime | None:
    """
    Parse Datto timestamps into naive Eastern for cutoff comparison.
    Supports ISO-8601 strings and Unix epoch as int/float (seconds or milliseconds).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(_ET).replace(tzinfo=None)
    if isinstance(value, (int, float)):
        try:
            n = float(value)
            if abs(n) > 1e12:
                n /= 1000.0
            dt = datetime.fromtimestamp(n, tz=timezone.utc)
            return dt.astimezone(_ET).replace(tzinfo=None)
        except (ValueError, OSError, OverflowError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return parse_datto_timestamp_to_et_naive(int(text))
        except (ValueError, OSError, OverflowError):
            return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(_ET).replace(tzinfo=None)
    except ValueError:
        return None


def effective_alert_timestamp_value(raw: dict, list_item: dict | None = None):
    """Prefer detail timestamp; fall back to list row (Datto may use ms int on both)."""
    for key in ("timestamp", "timeStamp"):
        v = raw.get(key)
        if v is not None:
            return v
    if list_item:
        for key in ("timestamp", "timeStamp"):
            v = list_item.get(key)
            if v is not None:
                return v
    return None


def alert_timestamp_from_list_item(item: dict) -> datetime | None:
    """Best-effort timestamp from a site alert list row (avoids detail fetch when clearly too old)."""
    for key in ("timestamp", "timeStamp", "raisedOn", "raisedTime", "createdOn", "alertTime", "time"):
        if key in item:
            ts = parse_datto_timestamp_to_et_naive(item.get(key))
            if ts is not None:
                return ts
    return None


def is_on_or_after_ingest_cutoff(alert_ts: datetime | None) -> bool:
    """False if missing timestamp (conservative) or before cutoff."""
    if alert_ts is None:
        return False
    return alert_ts >= get_ingest_cutoff_et_naive()
