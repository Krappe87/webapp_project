"""Live metrics during Datto alert sync (thread-safe for async workers + sync HTTP reads)."""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()


def _fresh_ingest_counters() -> dict[str, Any]:
    """Default ingest sub-state; keep in sync with fields updated in tick_ingest_done / start_ingest_phase."""
    return {
        "uids_queued": 0,
        "uids_done": 0,
        "ingested": 0,
        "skipped_existing": 0,
        "skipped_filter": 0,
        "skipped_before_cutoff": 0,
        "skipped_missing_timestamp": 0,
        "skipped_file_server": 0,
        "errors": 0,
        "current_uid": None,
    }


_state: dict[str, Any] = {
    "is_running": False,
    "run_started_at_epoch": None,
    "run_finished_at_epoch": None,
    "phase": "idle",
    "sites_total": 0,
    "site_index": 0,
    "current_site_uid": None,
    "current_state": None,
    "max_pages_per_state": None,
    "ingest_concurrency": 1,
    "listing": {
        "resolved_pages": 0,
        "open_pages": 0,
        "resolved_rows": 0,
        "open_rows": 0,
        "resolved_truncated": False,
        "open_truncated": False,
    },
    "ingest": _fresh_ingest_counters(),
    "last_completed_run": None,
}


def begin_run(*, total_sites: int, max_pages: int | None, concurrency: int) -> None:
    with _lock:
        _state["is_running"] = True
        _state["run_started_at_epoch"] = time.time()
        _state["run_finished_at_epoch"] = None
        _state["phase"] = "listing"
        _state["sites_total"] = total_sites
        _state["site_index"] = 0
        _state["current_site_uid"] = None
        _state["current_state"] = None
        _state["max_pages_per_state"] = max_pages
        _state["ingest_concurrency"] = concurrency
        _state["listing"] = {
            "resolved_pages": 0,
            "open_pages": 0,
            "resolved_rows": 0,
            "open_rows": 0,
            "resolved_truncated": False,
            "open_truncated": False,
        }
        _state["ingest"] = _fresh_ingest_counters()


def set_site(site_uid: str, site_index: int) -> None:
    with _lock:
        _state["current_site_uid"] = site_uid
        _state["site_index"] = site_index


def set_listing_state(state: str) -> None:
    with _lock:
        _state["phase"] = "listing"
        _state["current_state"] = state


def note_list_page(site_uid: str, state: str, pages_so_far: int, _batch: int, cumulative_rows: int) -> None:
    with _lock:
        _state["current_site_uid"] = site_uid
        _state["current_state"] = state
        if state == "resolved":
            _state["listing"]["resolved_pages"] = pages_so_far
            _state["listing"]["resolved_rows"] = cumulative_rows
        elif state == "open":
            _state["listing"]["open_pages"] = pages_so_far
            _state["listing"]["open_rows"] = cumulative_rows


def set_list_truncated(state: str, truncated: bool) -> None:
    with _lock:
        if state == "resolved":
            _state["listing"]["resolved_truncated"] = truncated
        elif state == "open":
            _state["listing"]["open_truncated"] = truncated


def start_ingest_phase(uids_queued: int) -> None:
    with _lock:
        _state["phase"] = "ingesting"
        _state["current_state"] = None
        _state["ingest"]["uids_queued"] = uids_queued


def set_current_uid(uid: str | None) -> None:
    with _lock:
        _state["ingest"]["current_uid"] = uid


def tick_ingest_done(status: str, detail: str | None) -> None:
    with _lock:
        ing = _state["ingest"]
        ing["uids_done"] += 1
        if status == "ingested":
            ing["ingested"] += 1
        elif status == "skipped":
            if detail == "not_cpu_or_memory_type":
                ing["skipped_filter"] += 1
            elif detail == "before_ingest_cutoff":
                ing["skipped_before_cutoff"] += 1
            elif detail == "missing_alert_timestamp":
                ing["skipped_missing_timestamp"] += 1
            elif detail == "skipped_file_server_hostname":
                ing["skipped_file_server"] += 1
            else:
                ing["skipped_existing"] += 1
        else:
            ing["errors"] += 1


def finalize_run(result: dict | None) -> None:
    with _lock:
        _state["is_running"] = False
        _state["phase"] = "idle"
        _state["run_finished_at_epoch"] = time.time()
        _state["ingest"]["current_uid"] = None
        snap = {
            "finished_at_epoch": _state["run_finished_at_epoch"],
            "started_at_epoch": _state["run_started_at_epoch"],
            "listing": dict(_state["listing"]),
            "ingest": dict(_state["ingest"]),
            "result": result,
        }
        _state["last_completed_run"] = snap


def get_snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "is_running": _state["is_running"],
            "run_started_at_epoch": _state["run_started_at_epoch"],
            "run_finished_at_epoch": _state["run_finished_at_epoch"],
            "phase": _state["phase"],
            "sites_total": _state["sites_total"],
            "site_index": _state["site_index"],
            "current_site_uid": _state["current_site_uid"],
            "current_state": _state["current_state"],
            "max_pages_per_state": _state["max_pages_per_state"],
            "ingest_concurrency": _state["ingest_concurrency"],
            "listing": dict(_state["listing"]),
            "ingest": dict(_state["ingest"]),
            "last_completed_run": _state["last_completed_run"],
        }
