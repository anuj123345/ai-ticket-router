"""
Health of the data pipeline, for the admin Pipeline page.

Reads public.pipeline_status() (bronze counts, gold counts, recent runs) and
turns it into checks a person can act on:
  * reconciliation - gold has exactly the tickets bronze has
  * lag            - changes in bronze not yet loaded into gold
  * freshness      - the scheduled dbt build ran recently
  * last build     - passed, with warnings, or failed
"""
from __future__ import annotations

from datetime import datetime, timezone

from models.db import get_client

STALE_AFTER_HOURS = 3     # dbt is scheduled hourly; three misses in a row is a problem


def _ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def ago(when: datetime | None, now: datetime) -> str:
    if when is None:
        return "never"
    s = max(0, int((now - when).total_seconds()))
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


def _n(count, word: str) -> str:
    count = count or 0
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


BUILD_LABEL = {"prod": "scheduled build", "ci": "CI build"}


def build(raw: dict, now: datetime | None = None) -> dict:
    """Pure: raw pipeline_status() JSON -> what the page shows."""
    now = now or datetime.now(timezone.utc)
    checks = []

    bronze = raw.get("bronze_tickets")
    gold = raw.get("gold_tickets")
    if gold is None:
        checks.append({"name": "Gold layer", "ok": False,
                       "text": "Not built yet. The first scheduled dbt run creates it."})
    else:
        checks.append({"name": "Reconciliation", "ok": bronze == gold,
                       "text": (f"Gold has all {gold:,} tickets that bronze has."
                                if bronze == gold else
                                f"Bronze has {bronze:,} tickets, gold has {gold:,}.")})
        b_max, g_max = _ts(raw.get("bronze_max_updated_at")), _ts(raw.get("gold_max_updated_at"))
        behind = b_max is not None and (g_max is None or b_max > g_max)
        checks.append({"name": "Lag", "ok": not behind,
                       "text": (f"Bronze changed {ago(b_max, now)}; gold last loaded a change from "
                                f"{ago(g_max, now)}. The next run picks it up."
                                if behind else "Gold includes the latest ticket change.")})

    last = raw.get("last_dbt") or {}
    finished = _ts(last.get("finished_at"))
    if not last:
        checks.append({"name": "Last dbt build", "ok": False, "text": "No dbt run recorded yet."})
    else:
        d = last.get("details") or {}
        ok = last.get("status") == "success"
        tests = d.get("tests", 0)
        text = (f'{last.get("status", "?").title()} {ago(finished, now)}: '
                f'{_n(d.get("models"), "model")}, {_n(tests, "test")}, '
                f'{d.get("failed", 0)} failed, {_n(d.get("warned"), "warning")}.')
        checks.append({"name": "Last dbt build", "ok": ok, "text": text,
                       "error": None if ok else last.get("error")})
        fresh = finished is not None and (now - finished).total_seconds() < STALE_AFTER_HOURS * 3600
        checks.append({"name": "Freshness", "ok": fresh,
                       "text": ("dbt ran within the last {} hours.".format(STALE_AFTER_HOURS) if fresh else
                                f"No dbt run for over {STALE_AFTER_HOURS} hours. "
                                "Check the GitHub Actions schedule.")})

    runs = []
    for r in raw.get("runs") or []:
        started, finished = _ts(r.get("started_at")), _ts(r.get("finished_at"))
        d = r.get("details") or {}
        if r.get("pipeline") == "dbt":
            summary = (f'{d.get("command", "")}: {_n(d.get("models"), "model")}, {_n(d.get("tests"), "test")}'
                       + (f', {d.get("failed")} failed' if d.get("failed") else "")
                       + (f', {_n(d.get("warned"), "warning")}' if d.get("warned") else ""))
        else:
            reason = d.get("reason")
            summary = {"duplicate": "Same file already uploaded; nothing changed",
                       "schema_change_unconfirmed": "Columns changed; waiting for confirmation"
                       }.get(reason, reason or "")
            if not summary and d.get("replaced"):
                summary = "Replaced the earlier version"
        runs.append({
            "pipeline": r.get("pipeline"),
            "name": (BUILD_LABEL.get(d.get("target"), "manual build")
                     if r.get("pipeline") == "dbt" else r.get("run_key")),
            "status": r.get("status"),
            "when": ago(started, now),
            "started_at": started.isoformat() if started else "",
            "seconds": round((finished - started).total_seconds(), 1) if started and finished else None,
            "rows": r.get("rows_out"),
            "summary": summary,
            "error": r.get("error"),
        })

    return {
        "checks": checks,
        "all_ok": all(c["ok"] for c in checks),
        "bronze_tickets": bronze,
        "gold_tickets": gold,
        "by_department": raw.get("by_department") or [],
        "runs": runs,
    }


def load() -> dict:
    raw = get_client().rpc("pipeline_status", {}).execute().data or {}
    return build(raw)
