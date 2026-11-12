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
            .select("id, document_id, document_name, sheet_name, sheet_index, row_count")
            .order("document_name").order("sheet_index")
            .execute()).data or []
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


# ── Files uploaded before the exact engine ───────────────────────────────────
# They live in the old dataset_rows table (one JSON object per row, file order
# by id). They are converted once, on first open, with no re-upload needed.

TABULAR_TYPES = ("xlsx", "xlsm", "xls", "csv", "tsv")
LEGACY_PAGE = 1000      # PostgREST's maximum rows per request


def legacy_documents() -> list[dict]:
    """Spreadsheet documents that still only have old-format rows."""
    client = get_client()
    docs = (client.table("documents").select("id, name, file_type")
            .execute()).data or []
    converted = {r["document_id"] for r in
                 (client.table("datasets").select("document_id").execute()).data or []}
    out = []
    for d in docs:
        if (d.get("file_type") or "").lower() not in TABULAR_TYPES or d["id"] in converted:
            continue
        probe = (client.table("dataset_rows").select("id")
                 .eq("document_id", d["id"]).limit(1).execute()).data
        if probe:
            out.append({"document_id": d["id"], "name": d["name"]})
    return out


def _legacy_column_order(keys: list[str], first_chunk: str) -> list[str]:
    """The old import kept no column order, but its first text chunk lists the
    columns in file order ("Name: value, ..." or "## Data (a, b, c)")."""
    import re
    pos = {}
    for k in keys:
        m = re.search(r"(?:^|[\s,(])" + re.escape(k) + r"(?::\s|,\s|\))", first_chunk)
        pos[k] = m.start() if m else None
    found = sorted((p, i, k) for i, k in enumerate(keys) if (p := pos[k]) is not None)
    missing = [k for k in keys if pos[k] is None]
    return [k for _, _, k in found] + missing


def convert_legacy(document_id: int) -> int:
    """Convert one old-format document into a dataset. Returns the dataset id."""
    client = get_client()
    doc = (client.table("documents").select("id, name, file_type")
           .eq("id", document_id).execute()).data
    if not doc:
        raise DatasetNotFound(f"Document {document_id} not found")
    name = doc[0]["name"]

    records: list[dict] = []
    start = 0
    while True:
        page = (client.table("dataset_rows").select("id, row_data")
                .eq("document_id", document_id).order("id")
                .range(start, start + LEGACY_PAGE - 1).execute()).data or []
        records.extend(r["row_data"] or {} for r in page)
        if len(page) < LEGACY_PAGE:
            break
        start += LEGACY_PAGE
    if not records:
        raise DatasetNotFound(f"Document {document_id} has no rows to convert")

    keys: list[str] = []
    seen: set = set()
    for r in records:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    chunk = (client.table("doc_chunks").select("content")
             .eq("document_id", document_id).eq("chunk_index", 0).execute()).data
    names = _legacy_column_order(keys, chunk[0]["content"] if chunk else "")

    rows: list[tuple[int, list]] = [(1, list(names))]
    for i, r in enumerate(records, start=2):
        cells = [tabular._excel_cell_text(r.get(k)) if not isinstance(r.get(k), str)
                 else tabular._clean_text(r[k]) for k in names]
        while cells and cells[-1] is None:
            cells.pop()
        if cells:
            rows.append((i, cells))

    a = tabular.analyse_sheet(RawSheet("", 0, rows), {"header_row": 1})
    a.report["warnings"].insert(0, {
        "level": "info",
        "msg": "Converted from an earlier import. Row numbers are positions in that import, "
               "not in your file. Re-upload the file for exact row numbers.",
    })
    a.report["converted_from_legacy"] = True
    return save_dataset(document_id, name, a, set(), {"header_row": 1})
