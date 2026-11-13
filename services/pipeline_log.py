"""
Run log for the app's own pipelines (uploads), written to public.pipeline_runs
next to dbt's rows. Best effort: a logging failure never fails the upload.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from models.db import get_client

log = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(timezone.utc)


def record(pipeline: str, run_key: str, status: str, started_at: datetime, *,
           rows_in: int | None = None, rows_out: int | None = None,
           details: dict | None = None, error: str | None = None) -> None:
    try:
        get_client().table("pipeline_runs").insert({
            "pipeline":    pipeline,
            "run_key":     (run_key or "")[:300],
            "status":      status,
            "started_at":  started_at.isoformat(),
            "finished_at": now().isoformat(),
            "rows_in":     rows_in,
            "rows_out":    rows_out,
            "details":     details or {},
            "error":       (error or None) and error[:4000],
        }).execute()
    except Exception as e:   # table not created yet, network blip...
        log.warning("Could not write pipeline run log (%s): %s", pipeline, type(e).__name__)
