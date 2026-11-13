"""
Database access for Data Studio datasets (tables: datasets, dataset_columns,
dataset_records; functions: ds_query, ds_rows, ds_get_records, ds_put_records).

All calculation happens in Postgres through ds_query(). This module only
moves data in and out; it never adds numbers up itself.
"""
from __future__ import annotations

import datetime as dt
import logging

from models.db import get_client
from services import tabular
from services.tabular import Analysed, RawSheet

log = logging.getLogger(__name__)

INSERT_BATCH = 1500     # records per insert request
PAGE         = 5000     # records per read/write page when settings change


class DatasetNotFound(LookupError):
    pass


# ── Writing ──────────────────────────────────────────────────────────────────

def save_dataset(document_id: int, document_name: str, a: Analysed,
                 percent_cols: set[int] | None = None, options: dict | None = None) -> int:
    """Store one analysed sheet. Returns the new dataset id."""
    client = get_client()
    opts = dict(options or {})
    opts["percent_cols"] = sorted(percent_cols or [])
    # Hidden until every row is stored: if the server is stopped half-way
    # (e.g. a time limit), a partial dataset must never show partial totals.
    opts["_loading"] = True
    resp = client.table("datasets").insert({
        "document_id":   document_id,
        "document_name": document_name,
        "sheet_name":    a.sheet_name,
        "sheet_index":   a.sheet_index,
        "header_row":    a.header_row,
        "row_count":     a.data_rows,
        "options":       opts,
        "report":        a.report,
    }).execute()
    dataset_id = resp.data[0]["id"]
    try:
        client.table("dataset_columns").insert(
            [{"dataset_id": dataset_id, **c.to_row()} for c in a.columns]).execute()
        batch: list[dict] = []
        for rec in a.records:
            batch.append({"dataset_id": dataset_id, **rec})
            if len(batch) >= INSERT_BATCH:
                client.table("dataset_records").insert(batch).execute()
                batch = []
        if batch:
            client.table("dataset_records").insert(batch).execute()
        opts.pop("_loading")
        client.table("datasets").update({"options": opts}).eq("id", dataset_id).execute()
    except Exception:
        # Never leave a half-loaded dataset behind: its totals would be wrong.
        log.exception("Saving dataset %s failed; removing it", dataset_id)
        client.table("datasets").delete().eq("id", dataset_id).execute()
        raise
    return dataset_id


def rebuild_dataset(dataset_id: int, changes: dict) -> dict:
    """
    Re-read a dataset with new settings (header row, column types, date order,
    total rows). Works from the stored file text, so no re-upload is needed.
    Returns the new import report.
    """
    client = get_client()
    ds = get_dataset(dataset_id)

    options = dict(ds["options"] or {})
    for key in ("header_row", "date_order", "include_totals"):
        if key in changes:
            if changes[key] is None:
                options.pop(key, None)
            else:
                options[key] = changes[key]
    if "columns" in changes:
        cols = dict(options.get("columns") or {})
        for pos, o in (changes["columns"] or {}).items():
            merged = {**cols.get(str(pos), {}), **{k: v for k, v in (o or {}).items()}}
            merged = {k: v for k, v in merged.items() if v not in (None, "", "auto")}
            if merged:
                cols[str(pos)] = merged
            else:
                cols.pop(str(pos), None)
        options["columns"] = cols
    if "header_row" in changes and changes["header_row"] != ds["header_row"]:
        # Column positions mean something else under a different header row.
        options["columns"] = {}

    rows: list[tuple[int, list]] = []
    after = 0
    while True:
        page = client.rpc("ds_get_records", {
            "p_dataset_id": dataset_id, "p_after": after, "p_limit": PAGE}).execute().data or []
        for row_num, raw in page:
            cells = list(raw)
            while cells and cells[-1] is None:
                cells.pop()
            rows.append((row_num, cells))
        if len(page) < PAGE:
            break
        after = page[-1][0]

    sheet = RawSheet(ds["sheet_name"], ds["sheet_index"], rows,
                     set(options.get("percent_cols") or []))
    a = tabular.analyse_sheet(sheet, options)

    # Columns first (ds_query reads types from here), then the values.
    client.table("dataset_columns").delete().eq("dataset_id", dataset_id).execute()
    client.table("dataset_columns").insert(
        [{"dataset_id": dataset_id, **c.to_row()} for c in a.columns]).execute()
    items = [[r["row_num"], r["status"], r["val"]] for r in a.records]
    for i in range(0, len(items), PAGE):
        client.rpc("ds_put_records", {
            "p_dataset_id": dataset_id, "p_items": items[i:i + PAGE]}).execute()
    client.table("datasets").update({
        "header_row": a.header_row, "row_count": a.data_rows,
        "options": options, "report": a.report,
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }).eq("id", dataset_id).execute()
    return a.report


# ── Reading ──────────────────────────────────────────────────────────────────

def list_datasets() -> list[dict]:
    client = get_client()
    rows = (client.table("datasets")
            .select("id, document_id, document_name, sheet_name, sheet_index, row_count, options")
            .order("document_name").order("sheet_index")
            .execute()).data or []
    rows = [r for r in rows if not (r.get("options") or {}).get("_loading")]
    for r in rows:
        r.pop("options", None)
    per_doc: dict[int, int] = {}
    for r in rows:
        per_doc[r["document_id"]] = per_doc.get(r["document_id"], 0) + 1
    for r in rows:
        multi = per_doc[r["document_id"]] > 1
        r["label"] = (f'{r["document_name"]} — {r["sheet_name"]}'
                      if multi and r["sheet_name"] else r["document_name"])
        r["name"] = r["document_name"]          # kept for older callers
    return rows


def get_dataset(dataset_id: int) -> dict:
    client = get_client()
    rows = (client.table("datasets").select("*").eq("id", dataset_id).execute()).data or []
    if not rows:
        raise DatasetNotFound(f"Dataset {dataset_id} not found")
    ds = rows[0]
    if (ds.get("options") or {}).get("_loading"):
        raise DatasetNotFound(f"Dataset {dataset_id} did not finish loading; re-upload the file")
    ds["columns"] = (client.table("dataset_columns").select("*")
                     .eq("dataset_id", dataset_id).order("pos").execute()).data or []
    return ds


def query(dataset_id: int, specs: list[dict]) -> list[dict]:
    """Run several ds_query specs in one round trip."""
    if not specs:
        return []
    return get_client().rpc("ds_query", {
        "p_dataset_id": dataset_id, "p_specs": specs}).execute().data


def rows(dataset_id: int, filters: list[dict] | None, limit: int = 100, offset: int = 0) -> dict:
    return get_client().rpc("ds_rows", {
        "p_dataset_id": dataset_id,
        "p_spec": {"filters": filters or [], "limit": limit, "offset": offset},
    }).execute().data


# ── Spreadsheet file types that become Data Studio datasets ──────────────────

TABULAR_TYPES = ("xlsx", "xlsm", "xls", "csv", "tsv")
