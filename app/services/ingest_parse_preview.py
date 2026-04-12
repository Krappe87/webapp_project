"""Read-only: how Datto list rows are parsed vs sync filters (page 0 + optional detail simulation)."""

from sqlalchemy.orm import Session

from app.models import Alert
from app.services.datto import (
    get_configured_site_uids,
    fetch_site_alert_list_page0,
    is_cpu_or_memory_performance_alert,
    extract_alert_uid_from_summary_dict,
    effective_alert_context,
)
from app.services.alert_ingest import SYNC_SITE_ALERT_STATE_ORDER, explain_datto_alert_ingest
from app.services.host_classification import (
    hostname_from_list_item,
    is_file_backup_server_hostname,
    is_citrix_hostname,
)
from app.services.ingest_cutoff import (
    get_ingest_cutoff_et_naive,
    alert_timestamp_from_list_item,
    is_on_or_after_ingest_cutoff,
)


def _list_row_type_digest(list_item: dict) -> dict[str, object]:
    """CPU/MEMORY hint from list row shape only (no detail GET). Detail may still change the merged type."""
    merged = effective_alert_context({}, list_item)
    typ = merged.get("type") or merged.get("Type")
    raw_min = {"alertContext": list_item.get("alertContext") or list_item.get("alert_context")}
    return {
        "merged_alert_context_type_list_only": str(typ).strip() if typ is not None else None,
        "passes_cpu_memory_list_shape_only": is_cpu_or_memory_performance_alert(raw_min, None),
    }


async def run_ingest_parse_preview(
    db: Session,
    site_uid: str | None,
    rows_per_state: int,
    detail_samples: int,
) -> dict:
    """
    For one site: fetch page 0 of each alert list state (same order as sync), show list-level parsing,
    then optionally run explain_datto_alert_ingest for the first N UIDs after the same list-stage
    dedupe/cutoff rules as sync — **page 0 only** (see merge_queue_scope in response).
    """
    sites = get_configured_site_uids()
    if not sites:
        return {
            "ok": False,
            "error": "no_sites_configured",
            "hint": "Set DATTO_SITE_UIDS in .env.",
        }

    chosen_site = site_uid.strip() if site_uid and site_uid.strip() else sites[0]
    if chosen_site not in sites:
        return {
            "ok": False,
            "error": "site_uid_not_in_DATTO_SITE_UIDS",
            "site_uid_requested": chosen_site,
            "configured_site_uids": sites,
        }

    cutoff = get_ingest_cutoff_et_naive()
    out: dict[str, object] = {
        "ok": True,
        "ingest_cutoff_et_naive": cutoff.isoformat(sep=" ", timespec="seconds"),
        "sync_site_alert_state_order": list(SYNC_SITE_ALERT_STATE_ORDER),
        "site_uid": chosen_site,
        "merge_queue_scope": (
            "UIDs are merged from **page 0 only** per state (fast). Full sync paginates all pages; "
            "alerts that appear only on page 1+ are omitted here."
        ),
        "states": {},
        "merged_page0_queue": [],
        "host_classification": (
            "CET-FILE-* hostnames are never ingested. "
            "Citrix-style hosts: hostname matches XA+digits or S + two digits (S16…S99), "
            "so new labels like S25 match without config changes."
        ),
    }

    state_pages: dict[str, dict] = {}
    for state in SYNC_SITE_ALERT_STATE_ORDER:
        page = await fetch_site_alert_list_page0(chosen_site, state)
        if page is None:
            state_pages[state] = {"fetch_ok": False, "items": [], "error": "page0_fetch_failed"}
            out["states"][state] = {
                "fetch_ok": False,
                "error": "page0_fetch_failed",
                "sample_rows": [],
            }
            continue
        state_pages[state] = {"fetch_ok": True, **page}
        items = page["items"]
        sample_slice = items[:rows_per_state]
        sample_uids = [
            extract_alert_uid_from_summary_dict(x)
            for x in sample_slice
            if isinstance(x, dict) and extract_alert_uid_from_summary_dict(x)
        ]
        existing_set: set[str] = set()
        if sample_uids:
            existing_set = {
                row[0]
                for row in db.query(Alert.alertuid).filter(Alert.alertuid.in_(sample_uids)).all()
            }

        sample_rows: list[dict[str, object]] = []
        for item in sample_slice:
            if not isinstance(item, dict):
                continue
            uid = extract_alert_uid_from_summary_dict(item)
            lst_ts = alert_timestamp_from_list_item(item)
            digest = _list_row_type_digest(item)
            skipped_list = lst_ts is not None and not is_on_or_after_ingest_cutoff(lst_ts)
            hn_guess = hostname_from_list_item(item)
            sample_rows.append(
                {
                    "alertUid": uid,
                    "list_row_key_count": len(item),
                    "list_row_keys_sample": sorted(item.keys())[:40],
                    "list_timestamp_parsed_et": (
                        lst_ts.isoformat(sep=" ", timespec="seconds") if lst_ts else None
                    ),
                    "list_passes_cutoff": (
                        is_on_or_after_ingest_cutoff(lst_ts) if lst_ts is not None else None
                    ),
                    "skipped_at_list_stage": skipped_list,
                    "skipped_at_list_reason": (
                        "list_timestamp_before_ingest_cutoff" if skipped_list else None
                    ),
                    "note_list_timestamp_missing": (
                        "Sync keeps the row when list timestamp is missing; cutoff is re-checked using detail timestamps."
                        if lst_ts is None
                        else None
                    ),
                    **digest,
                    "list_hostname_guess": hn_guess,
                    "would_skip_file_server": bool(hn_guess and is_file_backup_server_hostname(hn_guess)),
                    "would_be_citrix_host_from_hostname": bool(hn_guess and is_citrix_hostname(hn_guess)),
                    "already_in_db": bool(uid and uid in existing_set),
                }
            )

        out["states"][state] = {
            "fetch_ok": True,
            "page0_item_count": page["item_count"],
            "next_page_url_present": bool(page.get("next_page_url")),
            "payload_top_level_keys": page.get("payload_top_level_keys"),
            "sample_rows": sample_rows,
        }

    uids_to_process: list[str] = []
    uid_to_list_item: dict[str, dict] = {}
    for state in SYNC_SITE_ALERT_STATE_ORDER:
        sp = state_pages.get(state) or {}
        if not sp.get("fetch_ok"):
            continue
        for item in sp.get("items") or []:
            if not isinstance(item, dict):
                continue
            uid = extract_alert_uid_from_summary_dict(item)
            if not uid:
                continue
            if uid in uid_to_list_item:
                continue
            lst_ts = alert_timestamp_from_list_item(item)
            if lst_ts is not None and not is_on_or_after_ingest_cutoff(lst_ts):
                continue
            hn_row = hostname_from_list_item(item)
            if hn_row and is_file_backup_server_hostname(hn_row):
                continue
            uid_to_list_item[uid] = item
            uids_to_process.append(uid)

    out["merged_page0_queue"] = {
        "uid_count_after_list_filters": len(uids_to_process),
        "head_uids": uids_to_process[: min(50, len(uids_to_process))],
    }

    explains: list[dict[str, object]] = []
    n = min(detail_samples, len(uids_to_process))
    for uid in uids_to_process[:n]:
        li = uid_to_list_item.get(uid)
        explains.append(await explain_datto_alert_ingest(db, uid, li))
    out["detail_explain_samples"] = {
        "requested": detail_samples,
        "evaluated": n,
        "rows": explains,
    }

    return out
