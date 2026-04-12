import asyncio
import json
import os
import re
import httpx
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

ListPageCallback = Callable[[str, str, int, int, int], Awaitable[None]]

from app.services.datto_rate_limit import wait_for_datto_rate_limit

DATTO_API_URL = os.getenv("DATTO_API_URL", "https://concord-api.centrastage.net").rstrip("/")
API_KEY = os.getenv("DATTO_API_KEY", "")
API_SECRET = os.getenv("DATTO_API_SECRET", "")

# Comma-separated site UIDs (from GET /api/v2/account/sites → uid)
DATTO_SITE_UIDS = os.getenv("DATTO_SITE_UIDS", "")

# Doc: up to ~250 alerts per page for paginated site/account alert lists
_DATTO_ALERTS_PAGE_SIZE = 250


def get_alert_list_max_pages() -> int | None:
    """
    Max Datto list HTTP responses per site per state (open / resolved each have their own cap).
    Set DATTO_ALERT_LIST_MAX_PAGES=0 for unlimited (old behavior). Default 40 (~10k rows per state at 250/page).
    """
    raw = os.getenv("DATTO_ALERT_LIST_MAX_PAGES", "40").strip()
    if raw == "" or raw == "0":
        return None
    return max(1, int(raw))


def get_ingest_concurrency() -> int:
    """Parallel detail+ingest workers (each uses its own DB session). Default 1."""
    return max(1, int(os.getenv("DATTO_INGEST_CONCURRENCY", "1")))


@dataclass
class SiteAlertListResult:
    """Outcome of paginated site alert list fetch."""

    items: list[dict]
    pages_fetched: int
    truncated_by_max_pages: bool


def _datto_client_timeout() -> httpx.Timeout:
    """
    Datto alert list pages can be large and slow to stream; httpx's default read timeout (~5s)
    causes ReadTimeout against the API. Override with DATTO_HTTP_READ_TIMEOUT (seconds).
    """
    return httpx.Timeout(
        connect=float(os.getenv("DATTO_HTTP_CONNECT_TIMEOUT", "30")),
        read=float(os.getenv("DATTO_HTTP_READ_TIMEOUT", "180")),
        write=float(os.getenv("DATTO_HTTP_WRITE_TIMEOUT", "180")),
        pool=float(os.getenv("DATTO_HTTP_POOL_TIMEOUT", "30")),
    )


_DATTO_RETRYABLE = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ConnectTimeout,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
)


def _datto_http_max_retries() -> int:
    return max(1, int(os.getenv("DATTO_HTTP_MAX_RETRIES", "3")))


def _datto_http_retry_backoff_sec() -> float:
    return float(os.getenv("DATTO_HTTP_RETRY_BACKOFF_SEC", "1.5"))


def get_configured_site_uids() -> list[str]:
    """Site UIDs from DATTO_SITE_UIDS (comma-separated)."""
    if not DATTO_SITE_UIDS.strip():
        return []
    return [s.strip() for s in DATTO_SITE_UIDS.split(",") if s.strip()]


async def _get_datto_access_token_inner() -> str | None:
    if not API_KEY or not API_SECRET:
        return None
    token_url = f"{DATTO_API_URL}/auth/oauth/token"
    auth = ("public-client", "public")
    data = {"grant_type": "password", "username": API_KEY, "password": API_SECRET}
    timeout = _datto_client_timeout()
    backoff = _datto_http_retry_backoff_sec()
    for attempt in range(_datto_http_max_retries()):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(token_url, data=data, auth=auth)
            if response.status_code == 200:
                return response.json().get("access_token")
            return None
        except _DATTO_RETRYABLE:
            if attempt + 1 >= _datto_http_max_retries():
                return None
            await asyncio.sleep(backoff * (2**attempt))
    return None


async def get_datto_access_token() -> str | None:
    await wait_for_datto_rate_limit()
    return await _get_datto_access_token_inner()


def _resolve_datto_url(path_or_url: str) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    path = path_or_url if path_or_url.startswith("/") else f"/{path_or_url}"
    return f"{DATTO_API_URL}{path}"


async def _datto_request_get(url: str, headers: dict[str, str]) -> httpx.Response:
    """GET with Datto timeouts and retries on transient network errors."""
    timeout = _datto_client_timeout()
    backoff = _datto_http_retry_backoff_sec()
    last_exc: BaseException | None = None
    for attempt in range(_datto_http_max_retries()):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await client.get(url, headers=headers)
        except _DATTO_RETRYABLE as exc:
            last_exc = exc
            if attempt + 1 >= _datto_http_max_retries():
                break
            await asyncio.sleep(backoff * (2**attempt))
    assert last_exc is not None
    raise last_exc


def extract_alert_uid_from_summary_dict(item: dict) -> str | None:
    """UID from a site/account alert list row (Datto field names vary)."""
    if not isinstance(item, dict):
        return None
    for key in ("alertUid", "alertUID", "alert_uid", "uid", "alertId", "alert_id", "id"):
        v = item.get(key)
        if v is not None and str(v).strip():
            return str(v).strip()
    return None


async def datto_get_json_with_status(path_or_url: str) -> dict:
    """
    Same as datto_get_json but returns HTTP status and body prefix on failure (for diagnostics).
    Keys: ok (bool), http_status, json (if 200 and parseable), body_prefix (on error), phase.
    """
    await wait_for_datto_rate_limit()
    token = await _get_datto_access_token_inner()
    if not token:
        return {"ok": False, "phase": "token", "http_status": None, "detail": "missing_credentials_or_token_denied"}
    await wait_for_datto_rate_limit()
    url = _resolve_datto_url(path_or_url)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        response = await _datto_request_get(url, headers)
    except _DATTO_RETRYABLE as exc:
        return {
            "ok": False,
            "phase": "http_transport",
            "http_status": None,
            "detail": type(exc).__name__,
            "detail_message": str(exc) or repr(exc),
        }
    status = response.status_code
    if status != 200:
        return {
            "ok": False,
            "phase": "http",
            "http_status": status,
            "body_prefix": (response.text or "")[:500],
        }
    try:
        return {"ok": True, "http_status": status, "json": response.json()}
    except Exception as exc:
        return {
            "ok": False,
            "phase": "json_parse",
            "http_status": status,
            "parse_error": str(exc),
            "body_prefix": (response.text or "")[:500],
        }


async def datto_get_json(path_or_url: str) -> Any | None:
    """
    Authenticated GET. Pass a path like /api/v2/site/{uid}/alerts/open?page=0
    or a full nextPageUrl returned by Datto.
    Rate-limits both the token request and the GET.
    """
    await wait_for_datto_rate_limit()
    token = await _get_datto_access_token_inner()
    if not token:
        return None
    await wait_for_datto_rate_limit()
    url = _resolve_datto_url(path_or_url)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    response = await _datto_request_get(url, headers)
    if response.status_code == 200:
        return response.json()
    return None


def normalize_alerts_list_payload(payload: Any) -> list[dict]:
    """Turn Datto alert list response into a list of alert dicts."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in (
            "alerts",
            "data",
            "items",
            "results",
            "pageItems",
            "openAlerts",
            "resolvedAlerts",
            "content",
        ):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
    return []


def split_paginated_alert_response(payload: Any) -> tuple[list[dict], str | None]:
    """
    Returns (alerts, next_page_url_or_path).
    next ref may be a full URL or relative path from pageDetails.nextPageUrl.
    """
    items = normalize_alerts_list_payload(payload)
    next_ref: str | None = None
    if isinstance(payload, dict):
        pd = payload.get("pageDetails") or payload.get("page_details") or {}
        next_ref = pd.get("nextPageUrl") or pd.get("nextPageurl") or pd.get("next_page_url")
    return items, next_ref


async def fetch_site_alert_list_page0(site_uid: str, state: str) -> dict | None:
    """
    First page only (page=0) for a site's open or resolved list — fast for diagnostics / parse preview.
    Returns None if the HTTP request fails. Never follows nextPageUrl.
    """
    if state not in ("open", "resolved"):
        return None
    path = f"/api/v2/site/{site_uid}/alerts/{state}?page=0"
    payload = await datto_get_json(path)
    if payload is None:
        return None
    items, next_page_url = split_paginated_alert_response(payload)
    keys: list[str] = []
    if isinstance(payload, dict):
        keys = list(payload.keys())[:40]
    return {
        "items": items,
        "payload_top_level_keys": keys,
        "next_page_url": next_page_url,
        "item_count": len(items),
    }


async def fetch_site_alerts_for_state_all_pages(
    site_uid: str,
    state: str,
    *,
    max_pages: int | None = None,
    on_list_page: ListPageCallback | None = None,
) -> SiteAlertListResult | None:
    """
    Fetch paginated open or resolved alerts for a site.
    state: 'open' | 'resolved'
    Returns None if the first request fails.
    max_pages: cap successful list responses (None = unlimited). Use get_alert_list_max_pages() from env.
    on_list_page: async (site_uid, state, pages_fetched_so_far, items_this_page, cumulative_item_count).
    """
    if state not in ("open", "resolved"):
        return None

    all_items: list[dict] = []
    next_query: str | None = f"/api/v2/site/{site_uid}/alerts/{state}?page=0"
    current_page = 0
    pages_done = 0
    truncated = False

    while next_query:
        if max_pages is not None and pages_done >= max_pages:
            truncated = True
            break

        payload = await datto_get_json(next_query)
        if payload is None:
            if not all_items:
                return None
            return SiteAlertListResult(
                items=all_items,
                pages_fetched=pages_done,
                truncated_by_max_pages=truncated,
            )

        items, next_page_url = split_paginated_alert_response(payload)
        all_items.extend(items)
        pages_done += 1
        if on_list_page:
            await on_list_page(site_uid, state, pages_done, len(items), len(all_items))

        if next_page_url:
            next_query = next_page_url
            if max_pages is not None and pages_done >= max_pages:
                truncated = True
                next_query = None
            continue

        if len(items) < _DATTO_ALERTS_PAGE_SIZE:
            break

        current_page += 1
        if current_page > 500:
            break
        next_query = f"/api/v2/site/{site_uid}/alerts/{state}?page={current_page}"

    return SiteAlertListResult(
        items=all_items,
        pages_fetched=pages_done,
        truncated_by_max_pages=truncated,
    )


async def fetch_datto_diagnostics(alertuid: str) -> dict:
    """Fetch full alert detail from the Datto RMM API."""
    data = await datto_get_json(f"/api/v2/alert/{alertuid}")
    return data if isinstance(data, dict) else {}


def _coerce_alert_context(ctx: Any) -> dict:
    if ctx is None:
        return {}
    if isinstance(ctx, str):
        try:
            ctx = json.loads(ctx)
        except json.JSONDecodeError:
            return {}
    return ctx if isinstance(ctx, dict) else {}


def effective_alert_context(raw: dict, list_item: dict | None) -> dict:
    """
    Merge alertContext from list row with detail GET.
    List responses often include full context; detail sometimes omits `type` — list fills gaps.
    """
    detail_ctx = _coerce_alert_context(raw.get("alertContext") or raw.get("alert_context"))
    if not list_item:
        return detail_ctx
    list_ctx = _coerce_alert_context(list_item.get("alertContext") or list_item.get("alert_context"))
    return {**list_ctx, **detail_ctx}


def effective_alert_source_info(raw: dict, list_item: dict | None) -> dict:
    """Merge alertSourceInfo (detail wins over list)."""
    d = raw.get("alertSourceInfo") or {}
    d = d if isinstance(d, dict) else {}
    if not list_item:
        return d
    li = list_item.get("alertSourceInfo") or {}
    li = li if isinstance(li, dict) else {}
    return {**li, **d}


def _alert_context_type_from_context(context: dict) -> str | None:
    if not context:
        return None
    t = context.get("type") or context.get("Type")
    if t is None:
        return None
    return str(t).strip().upper()


def derive_alert_category_from_raw(raw: dict, list_item: dict | None = None) -> str:
    """Best-effort category label from alertContext (e.g. @class)."""
    context = effective_alert_context(raw, list_item)
    cls = context.get("@class") or context.get("class")
    if cls:
        return str(cls)
    return ""


def normalize_diagnostics_for_storage(diagnostics: str | None) -> str | None:
    """
    Normalize Datto diagnostics text for storage and display.
    Converts CRLF/CR to LF, trims each line, drops trailing blank lines.
    """
    if diagnostics is None:
        return None
    if not isinstance(diagnostics, str):
        diagnostics = str(diagnostics)
    text = diagnostics.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    out = "\n".join(lines)
    return out if out else None


def is_cpu_or_memory_performance_alert(raw: dict, list_item: dict | None = None) -> bool:
    """
    True when alertContext.type is CPU or MEMORY (Datto performance alerts).
    RAM is treated as an alias for MEMORY. Merges list-row context when detail omits `type`.
    """
    if not raw:
        return False
    ctx = effective_alert_context(raw, list_item)
    nt = _alert_context_type_from_context(ctx)
    if not nt:
        return False
    if nt == "RAM":
        nt = "MEMORY"
    return nt in ("CPU", "MEMORY")


def reduce_datto_payload_for_storage(raw: dict | None, list_item: dict | None = None) -> dict:
    """Reduce raw Datto API response to the subset we store (optional list row for merge)."""
    if not raw:
        return {}
    source = effective_alert_source_info(raw, list_item)
    context = effective_alert_context(raw, list_item)
    diag = raw.get("diagnostics")
    if diag is None and list_item:
        diag = list_item.get("diagnostics")
    return {
        "alertUid": raw.get("alertUid"),
        "priority": raw.get("priority"),
        "diagnostics": normalize_diagnostics_for_storage(diag),
        "resolved": raw.get("resolved") if raw.get("resolved") is not None else (list_item.get("resolved") if list_item else None),
        "resolvedBy": raw.get("resolvedBy"),
        "resolvedOn": raw.get("resolvedOn"),
        "ticketNumber": raw.get("ticketNumber"),
        "timestamp": raw.get("timestamp") if raw.get("timestamp") is not None else (list_item.get("timestamp") if list_item else None),
        "alertSourceInfo": source if source else None,
        "alertContext": context if context else None,
        "device_hostname": source.get("deviceName"),
        "alert_type": context.get("type") or context.get("Type"),
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
