"""Ingest alerts from Datto RMM API (site-scoped polling; CPU/RAM performance only)."""

import asyncio

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func

from app.database import SessionLocal
from app.models import Client, Device, Alert
from app.services.datto import (
    get_configured_site_uids,
    fetch_site_alerts_for_state_all_pages,
    get_alert_list_max_pages,
    get_ingest_concurrency,
    fetch_datto_diagnostics,
    reduce_datto_payload_for_storage,
    derive_alert_category_from_raw,
    is_cpu_or_memory_performance_alert,
    extract_alert_uid_from_summary_dict,
    effective_alert_source_info,
    effective_alert_context,
)
from app.services.rules import run_rule_engine
from app.services.utils import extract_client_id
from app.services import sync_progress
from app.services.ingest_cutoff import (
    get_ingest_cutoff_et_naive,
    parse_datto_timestamp_to_et_naive,
    alert_timestamp_from_list_item,
    is_on_or_after_ingest_cutoff,
    effective_alert_timestamp_value,
)

# Order matches site list iteration in sync (resolved first lets resolved win uid_to_list_item when deduping).
SYNC_SITE_ALERT_STATE_ORDER: tuple[str, ...] = ("resolved", "open")


def _ensure_client_device(db: Session, hostname: str, client_id: str) -> None:
    client = db.query(Client).filter(Client.id == client_id).first()
    if not client:
        db.add(Client(id=client_id, name=f"Client {client_id}"))
    device = db.query(Device).filter(Device.hostname == hostname).first()
    if not device:
        db.add(Device(hostname=hostname, client_id=client_id))


def _hostname_from_raw(raw: dict, list_item: dict | None = None) -> str | None:
    if not raw:
        return None
    reduced = reduce_datto_payload_for_storage(raw, list_item)
    if reduced.get("device_hostname"):
        return reduced["device_hostname"]
    source = effective_alert_source_info(raw, list_item)
    return source.get("deviceName") or source.get("device_name")


async def ingest_single_alert_from_datto(
    db: Session, alertuid: str, list_item: dict | None = None
) -> tuple[str, str | None]:
    """
    Fetch full alert from Datto, keep only alertContext.type CPU or MEMORY, reduce payload, insert if new.
    Returns (status, detail) where status is 'ingested' | 'skipped' | 'error'.
    """
    existing = db.query(Alert).filter(Alert.alertuid == alertuid).first()
    if existing:
        return "skipped", None

    raw = await fetch_datto_diagnostics(alertuid)
    if not raw:
        return "error", "detail_fetch_failed"

    alert_ts = parse_datto_timestamp_to_et_naive(effective_alert_timestamp_value(raw, list_item))
    if alert_ts is None:
        return "skipped", "missing_alert_timestamp"
    if not is_on_or_after_ingest_cutoff(alert_ts):
        return "skipped", "before_ingest_cutoff"

    if not is_cpu_or_memory_performance_alert(raw, list_item):
        return "skipped", "not_cpu_or_memory_type"

    hostname = _hostname_from_raw(raw, list_item)
    if not hostname:
        return "error", "no_device_hostname"

    client_id = extract_client_id(hostname)
    diagnostic_data = reduce_datto_payload_for_storage(raw, list_item)
    alert_type = diagnostic_data.get("alert_type") or ""
    alert_category = derive_alert_category_from_raw(raw, list_item)
    resolved = raw.get("resolved")
    if resolved is None and list_item is not None:
        resolved = list_item.get("resolved")
    status = "Resolved" if resolved else "Open"

    _ensure_client_device(db, hostname, client_id)
    db.flush()

    new_alert = Alert(
        alertuid=alertuid,
        device_hostname=hostname,
        alert_type=alert_type,
        alert_category=alert_category,
        diagnostic_data=diagnostic_data,
        status=status,
        timestamp=alert_ts,
    )
    db.add(new_alert)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return "skipped", None

    run_rule_engine(client_id, db)
    return "ingested", None


async def explain_datto_alert_ingest(db: Session, alertuid: str, list_item: dict | None) -> dict:
    """
    Same decision order as ingest_single_alert_from_datto but read-only (no writes).
    Use for parse-preview / debugging why an alert would be ingested or skipped.
    """
    out: dict[str, object] = {"alertuid": alertuid}
    existing = db.query(Alert).filter(Alert.alertuid == alertuid).first()
    if existing:
        out["outcome"] = "skipped"
        out["detail"] = "already_in_db"
        return out

    raw = await fetch_datto_diagnostics(alertuid)
    out["detail_fetch_ok"] = bool(raw)
    if not raw:
        out["outcome"] = "error"
        out["detail"] = "detail_fetch_failed"
        return out

    ts_raw = effective_alert_timestamp_value(raw, list_item)
    out["effective_timestamp_raw"] = ts_raw
    alert_ts = parse_datto_timestamp_to_et_naive(ts_raw)
    out["timestamp_parsed_et_naive"] = (
        alert_ts.isoformat(sep=" ", timespec="seconds") if alert_ts else None
    )
    if alert_ts is None:
        out["outcome"] = "skipped"
        out["detail"] = "missing_alert_timestamp"
        return out

    passes_cutoff = is_on_or_after_ingest_cutoff(alert_ts)
    out["passes_ingest_cutoff"] = passes_cutoff
    if not passes_cutoff:
        out["outcome"] = "skipped"
        out["detail"] = "before_ingest_cutoff"
        return out

    merged_ctx = effective_alert_context(raw, list_item)
    typ = merged_ctx.get("type") or merged_ctx.get("Type")
    out["merged_alert_context_type"] = str(typ).strip() if typ is not None else None

    passes_type = is_cpu_or_memory_performance_alert(raw, list_item)
    out["passes_cpu_memory_filter"] = passes_type
    if not passes_type:
        out["outcome"] = "skipped"
        out["detail"] = "not_cpu_or_memory_type"
        return out

    hostname = _hostname_from_raw(raw, list_item)
    out["resolved_hostname"] = hostname
    if not hostname:
        out["outcome"] = "error"
        out["detail"] = "no_device_hostname"
        return out

    out["outcome"] = "would_ingest"
    out["detail"] = None
    return out


async def sync_alerts_from_datto(trace_limit: int = 0) -> dict:
    """
    For each configured site UID, pull open and resolved alert lists (all pages),
    then fetch each alert detail and ingest rows where alertContext.type is CPU or MEMORY.
    """
    import traceback

    try:
        return await _sync_alerts_from_datto_impl(trace_limit)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "traceback": traceback.format_exc()[:4000],
        }


async def _sync_alerts_from_datto_impl(trace_limit: int = 0) -> dict:
    site_uids = get_configured_site_uids()
    max_pages = get_alert_list_max_pages()
    ingest_concurrency = get_ingest_concurrency()
    sync_progress.begin_run(
        total_sites=len(site_uids),
        max_pages=max_pages,
        concurrency=ingest_concurrency,
    )
    out: dict | None = None
    try:
        if not site_uids:
            out = {
                "ok": False,
                "error": "no_sites_configured",
                "hint": "Set DATTO_SITE_UIDS in .env (comma-separated site UIDs).",
                "ingest_cutoff_since": get_ingest_cutoff_et_naive().isoformat(sep=" ", timespec="seconds"),
                "ingested": 0,
                "skipped_existing": 0,
                "skipped_not_cpu_or_memory_type": 0,
                "skipped_before_cutoff": 0,
                "skipped_list_before_cutoff": 0,
                "skipped_missing_alert_timestamp": 0,
                "errors": [],
                "sites": [],
                "max_pages_per_state": max_pages,
                "ingest_concurrency": ingest_concurrency,
            }
            return out

        ingested = 0
        skipped_existing = 0
        skipped_filter = 0
        skipped_before_cutoff = 0
        skipped_list_before_cutoff = 0
        skipped_missing_timestamp = 0
        errors: list[dict] = []
        site_summaries: list[dict] = []
        cutoff = get_ingest_cutoff_et_naive()
        trace: list[dict] = []

        db0 = SessionLocal()
        try:
            alerts_total_before = db0.query(func.count(Alert.alertuid)).scalar() or 0
        finally:
            db0.close()

        for site_i, site_uid in enumerate(site_uids):
            sync_progress.set_site(site_uid, site_i)
            site_entry: dict[str, object] = {
                "siteUid": site_uid,
                "open": 0,
                "resolved": 0,
                "uids_seen": 0,
                "skipped_list_before_cutoff": 0,
            }
            uids_to_process: list[str] = []
            uid_to_list_item: dict[str, dict] = {}

            for state in SYNC_SITE_ALERT_STATE_ORDER:
                sync_progress.set_listing_state(state)

                async def _on_list_page(s: str, st: str, pages: int, n_batch: int, cum: int) -> None:
                    sync_progress.note_list_page(s, st, pages, n_batch, cum)

                list_res = await fetch_site_alerts_for_state_all_pages(
                    site_uid,
                    state,
                    max_pages=max_pages,
                    on_list_page=_on_list_page,
                )
                if list_res is None:
                    errors.append({"siteUid": site_uid, "state": state, "error": "list_fetch_failed"})
                    continue

                items = list_res.items
                sync_progress.set_list_truncated(state, list_res.truncated_by_max_pages)
                site_entry[f"{state}_list_pages"] = list_res.pages_fetched
                site_entry[f"{state}_list_truncated"] = list_res.truncated_by_max_pages

                if state == "open":
                    site_entry["open"] = len(items)
                else:
                    site_entry["resolved"] = len(items)

                for item in items:
                    if not isinstance(item, dict):
                        continue
                    uid = extract_alert_uid_from_summary_dict(item)
                    if not uid:
                        continue
                    if uid in uids_to_process:
                        continue
                    lst_ts = alert_timestamp_from_list_item(item)
                    if lst_ts is not None and not is_on_or_after_ingest_cutoff(lst_ts):
                        skipped_list_before_cutoff += 1
                        site_entry["skipped_list_before_cutoff"] = (
                            int(site_entry["skipped_list_before_cutoff"]) + 1
                        )
                        continue
                    uid_to_list_item[uid] = item
                    uids_to_process.append(uid)

            site_entry["uids_seen"] = len(uids_to_process)

            sync_progress.start_ingest_phase(len(uids_to_process))

            sem = asyncio.Semaphore(ingest_concurrency)

            async def _ingest_uid(uid: str) -> tuple[str, str, str | None]:
                db = SessionLocal()
                try:
                    status, detail = await ingest_single_alert_from_datto(
                        db, uid, uid_to_list_item.get(uid)
                    )
                    return uid, status, detail
                finally:
                    db.close()

            async def _bounded(uid: str) -> tuple[str, str, str | None]:
                async with sem:
                    sync_progress.set_current_uid(uid)
                    return await _ingest_uid(uid)

            _CHUNK = 64
            for j in range(0, len(uids_to_process), _CHUNK):
                chunk = uids_to_process[j : j + _CHUNK]
                batch = await asyncio.gather(*[_bounded(u) for u in chunk])
                for uid, status, detail in batch:
                    sync_progress.tick_ingest_done(status, detail)
                    if trace_limit and len(trace) < trace_limit:
                        trace.append({"alertUid": uid, "status": status, "detail": detail})
                    if status == "ingested":
                        ingested += 1
                    elif status == "skipped":
                        if detail == "not_cpu_or_memory_type":
                            skipped_filter += 1
                        elif detail == "before_ingest_cutoff":
                            skipped_before_cutoff += 1
                        elif detail == "missing_alert_timestamp":
                            skipped_missing_timestamp += 1
                        else:
                            skipped_existing += 1
                    else:
                        errors.append({"alertUid": uid, "error": detail or status})

            site_summaries.append(site_entry)

        db1 = SessionLocal()
        try:
            alerts_total_after = db1.query(func.count(Alert.alertuid)).scalar() or 0
        finally:
            db1.close()

        out = {
            "ok": True,
            "ingest_cutoff_since": cutoff.isoformat(sep=" ", timespec="seconds"),
            "ingested": ingested,
            "skipped_existing": skipped_existing,
            "skipped_not_cpu_or_memory_type": skipped_filter,
            "skipped_before_cutoff": skipped_before_cutoff,
            "skipped_list_before_cutoff": skipped_list_before_cutoff,
            "skipped_missing_alert_timestamp": skipped_missing_timestamp,
            "errors": errors,
            "sites": site_summaries,
            "alerts_total_in_db_before": alerts_total_before,
            "alerts_total_in_db_after": alerts_total_after,
            "alerts_row_delta": alerts_total_after - alerts_total_before,
            "max_pages_per_state": max_pages,
            "ingest_concurrency": ingest_concurrency,
        }
        if trace_limit and trace:
            out["trace"] = trace
        return out
    finally:
        sync_progress.finalize_run(out)

