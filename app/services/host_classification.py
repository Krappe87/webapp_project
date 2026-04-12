"""Hostname rules: file/backup servers to drop, Citrix vs general infrastructure for reporting."""

import re

from app.services.datto import effective_alert_source_info

# Backup/file servers (CET-FILE-*) — high noise during backups; do not ingest alerts.
_FILE_SERVER_PREFIX = re.compile(r"(?i)^CET-FILE-")

# Citrix / RDS / XenApp style markers in hostnames (substring match, case-insensitive):
# - XA + digits: XenApp / CVAD (e.g. XA7, XA65)
# - S + exactly two digits: common Windows Server / SAC style labels (S16, S19, S22, S25, S28, …)
#   Extend automatically as new OS labels appear without code changes.
_CITRIX_MARKER = re.compile(r"(?i)(XA\d+|S\d{2})")


def is_file_backup_server_hostname(hostname: str | None) -> bool:
    """True for CET-FILE-* hosts — excluded from ingestion entirely."""
    if not hostname or not str(hostname).strip():
        return False
    return bool(_FILE_SERVER_PREFIX.match(hostname.strip()))


def is_citrix_hostname(hostname: str | None) -> bool:
    """
    True when hostname suggests a Citrix / session-host style server (vs SQL, APP, etc.).
    Uses generic patterns so new labels (e.g. S25) match without config updates.
    """
    if not hostname or not str(hostname).strip():
        return False
    return bool(_CITRIX_MARKER.search(hostname.strip()))


def hostname_from_list_item(list_item: dict | None) -> str | None:
    """Device name from a Datto list row only (no detail GET)."""
    if not list_item:
        return None
    source = effective_alert_source_info({}, list_item)
    name = source.get("deviceName") or source.get("device_name")
    return str(name).strip() if name else None
