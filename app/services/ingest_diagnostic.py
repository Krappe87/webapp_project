"""One-call troubleshooting for Datto alert ingestion (read-only)."""

from app.services.datto import (
    datto_get_json_with_status,
    normalize_alerts_list_payload,
    extract_alert_uid_from_summary_dict,
    fetch_datto_diagnostics,
    is_cpu_or_memory_performance_alert,
    get_configured_site_uids,
    effective_alert_context,
)
from app.services.ingest_cutoff import (
    get_ingest_cutoff_et_naive,
    parse_datto_timestamp_to_et_naive,
    alert_timestamp_from_list_item,
    is_on_or_after_ingest_cutoff,
    effective_alert_timestamp_value,
)


async def run_ingest_diagnostic() -> dict:
    """
    Hit Datto for the first configured site, first page of open alerts only,
    then one alert detail if a UID can be found. Surfaces common misconfigurations.
    """
    sites = get_configured_site_uids()
    cutoff = get_ingest_cutoff_et_naive()
    out: dict = {
        "ingest_cutoff_et_naive": cutoff.isoformat(sep=" ", timespec="seconds"),
        "site_uids": sites,
        "steps": [],
    }
    if not sites:
        out["summary"] = "No DATTO_SITE_UIDS configured."
        return out

    site_uid = sites[0]
    path = f"/api/v2/site/{site_uid}/alerts/open?page=0"
    http_result = await datto_get_json_with_status(path)
    step1: dict = {
        "description": "First-page site open alerts",
        "path": path,
        "ok": http_result.get("ok"),
        "http_status": http_result.get("http_status"),
        "phase": http_result.get("phase"),
    }
    if not http_result.get("ok"):
        step1["body_prefix"] = http_result.get("body_prefix")
        step1["parse_error"] = http_result.get("parse_error")
    elif isinstance(http_result.get("json"), dict):
        step1["payload_top_level_keys"] = list(http_result["json"].keys())[:50]
    out["steps"].append(step1)

    if not http_result.get("ok"):
        out["summary"] = (
            "List request failed. Check DATTO_API_URL for your region, site UID, and API credentials. "
            "See http_status and body_prefix."
        )
        return out

    payload = http_result.get("json")
    if isinstance(payload, dict):
        out["payload_top_level_keys"] = list(payload.keys())[:50]

    items = normalize_alerts_list_payload(payload)
    out["normalized_open_alert_count_page0"] = len(items)

    if not items:
        out["summary"] = (
            "HTTP 200 but zero alerts parsed. Datto JSON shape may differ — check payload_top_level_keys "
            "in Swagger for GET /api/v2/site/{siteUid}/alerts/open and extend normalize_alerts_list_payload if needed."
        )
        return out

    first = items[0]
    uid = extract_alert_uid_from_summary_dict(first)
    out["first_list_row_keys"] = list(first.keys())[:40]
    out["first_row_extracted_uid"] = uid
    lst_ts = alert_timestamp_from_list_item(first)
    out["first_row_list_timestamp_parsed"] = lst_ts.isoformat(sep=" ", timespec="seconds") if lst_ts else None
    out["first_row_passes_cutoff_from_list"] = is_on_or_after_ingest_cutoff(lst_ts) if lst_ts is not None else None

    if not uid:
        out["summary"] = (
            "Could not read alert UID from list rows. Check first_list_row_keys and add the correct key to "
            "extract_alert_uid_from_summary_dict in app/services/datto.py."
        )
        return out

    raw = await fetch_datto_diagnostics(uid)
    out["sample_alert_uid"] = uid
    out["detail_fetch_empty"] = not bool(raw)
    if not raw:
        out["summary"] = "List returned UIDs but GET /api/v2/alert/{uid} returned empty — check permissions or UID format."
        return out

    raw_ts = effective_alert_timestamp_value(raw, first)
    parsed = parse_datto_timestamp_to_et_naive(raw_ts)
    merged_ctx = effective_alert_context(raw, first)
    type_raw = merged_ctx.get("type") or merged_ctx.get("Type")

    out["detail"] = {
        "timestamp_raw_effective": raw_ts,
        "timestamp_parsed_et_naive": parsed.isoformat(sep=" ", timespec="seconds") if parsed else None,
        "passes_cutoff": is_on_or_after_ingest_cutoff(parsed) if parsed is not None else False,
        "alertContext_type_merged": type_raw,
        "passes_cpu_memory_filter": is_cpu_or_memory_performance_alert(raw, first),
        "resolved": raw.get("resolved"),
    }

    reasons = []
    if parsed is None:
        reasons.append(
            "No parseable timestamp (detail or list row). Datto often sends Unix ms integers — now supported."
        )
    elif not is_on_or_after_ingest_cutoff(parsed):
        reasons.append(f"Alert time is before ingest cutoff ({cutoff}).")
    if not is_cpu_or_memory_performance_alert(raw, first):
        reasons.append(
            f"Merged alertContext.type is {type_raw!r} — must be CPU or MEMORY (RAM counts as MEMORY)."
        )

    if reasons:
        out["summary"] = "Detail fetch OK but this sample would NOT ingest: " + " ".join(reasons)
    else:
        out["summary"] = "This sample alert would pass filters; if sync still ingests 0, run POST /api/datto/sync-alerts and check skipped_* counts or DB for existing rows."

    return out
