"""
Document Processor -- parses PDF, DOCX, TXT/MD, XLSX, CSV files,
chunks the text, generates NVIDIA embeddings (pgvector), and stores
in Supabase for semantic + full-text search fallback.
All processing happens in-memory (no disk writes -- Vercel compatible).
"""
from __future__ import annotations
import hashlib
import io
import logging
import os
import re
import time
from models.db import get_client
from services import ai_models
from services.ai_models import ModelUnavailableError

log = logging.getLogger(__name__)

EMBED_BATCH_SIZE  = 50     # texts per NVIDIA NIM call
CHUNK_INSERT_SIZE = 200    # text-only chunk rows per insert
EMBED_TIMEOUT_S   = 12.0   # per NIM call
EMBED_ATTEMPTS    = 2
RETRY_PAUSE_S     = 1.5
# Worst case for one batch: every attempt times out.
WORST_BATCH_S     = EMBED_ATTEMPTS * EMBED_TIMEOUT_S + RETRY_PAUSE_S   # 25.5s
REQUEST_LIMIT_S   = 52.0   # stay clear of Vercel's 60s maxDuration


# -- Embeddings ---------------------------------------------------------------

def _embed_client():
    # We retry ourselves, inside a time budget, so the SDK must not.
    return ai_models.client(timeout=EMBED_TIMEOUT_S, max_retries=0)


def _embed_batch(client, batch: list[str], attempts: int = EMBED_ATTEMPTS) -> list[list[float]] | None:
    """
    Embed one batch of passages. Retries temporary failures (timeouts, 5xx,
    rate limits) once and returns None if they persist. Permanent failures
    (retired model, bad key) raise ModelUnavailableError straight away:
    retrying them only burns time.
    """
    for attempt in range(attempts):
        try:
            return ai_models.embed(batch, "passage", nim=client)
        except ModelUnavailableError:
            raise
        except Exception as e:
            log.warning("Embedding batch failed (attempt %d/%d): %s",
                        attempt + 1, attempts, ai_models.describe_error(e))
            if attempt + 1 < attempts:
                time.sleep(RETRY_PAUSE_S)
    return None


def query_embedding(text: str) -> list[float] | None:
    """Query-side embedding for semantic search, or None (keyword search only)."""
    return ai_models.query_embedding(text)


# -- Text extraction ----------------------------------------------------------

def extract_text_pdf(file_bytes: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(file_bytes))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text.strip())
    return "\n\n".join(pages)


def extract_text_docx(file_bytes: bytes) -> str:
    from docx import Document
    doc = Document(io.BytesIO(file_bytes))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return "\n\n".join(paragraphs)


def extract_text_plain(file_bytes: bytes) -> str:
    # Detect UTF-16 BOM and decode accordingly
    if file_bytes[:2] in (b'\xff\xfe', b'\xfe\xff'):
        text = file_bytes.decode("utf-16", errors="replace")
    elif file_bytes[:3] == b'\xef\xbb\xbf':
        text = file_bytes[3:].decode("utf-8", errors="replace")
    else:
        text = file_bytes.decode("utf-8", errors="replace")
    # Strip null bytes -- PostgreSQL rejects null bytes in text columns
    return text.replace("\x00", "")


def extract_text_xlsx(file_bytes: bytes) -> str:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    sections = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue
        headers = [str(h).strip() if h is not None else f"Column{i+1}"
                   for i, h in enumerate(rows[0])]
        lines = [f"## {sheet_name}"]
        for row in rows[1:]:
            if all(cell is None or str(cell).strip() == "" for cell in row):
                continue
            parts = []
            for header, cell in zip(headers, row):
                if cell is not None and str(cell).strip() != "":
                    parts.append(f"{header}: {cell}")
            if parts:
                lines.append(", ".join(parts) + ".")
        if len(lines) > 1:
            sections.append("\n".join(lines))
    wb.close()
    return "\n\n".join(sections)


def extract_text_csv(file_bytes: bytes) -> str:
    import csv
    # Handle UTF-16 encoded files (common with Excel-exported TSV/CSV)
    if file_bytes[:2] in (b'\xff\xfe', b'\xfe\xff'):
        text = file_bytes.decode("utf-16", errors="replace")
    elif file_bytes[:3] == b'\xef\xbb\xbf':
        text = file_bytes[3:].decode("utf-8", errors="replace")
    else:
        text = file_bytes.decode("utf-8", errors="replace")
    text = text.replace("\x00", "")

    # Auto-detect delimiter: tab or comma
    sample = text[:2000]
    delimiter = "\t" if sample.count("\t") > sample.count(",") else ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    lines = []
    headers = reader.fieldnames or []
    if headers:
        lines.append(f"## Data ({', '.join(headers)})")
    for row in reader:
        parts = [f"{k}: {v}" for k, v in row.items() if v and str(v).strip()]
        if parts:
            lines.append(", ".join(parts) + ".")
    return "\n".join(lines)


def extract_text(file_bytes: bytes, file_type: str) -> str:
    ft = file_type.lower()
    if ft == "pdf":
        return extract_text_pdf(file_bytes)
    elif ft == "docx":
        return extract_text_docx(file_bytes)
    elif ft in ("xlsx", "xls"):
        return extract_text_xlsx(file_bytes)
    elif ft == "csv":
        return extract_text_csv(file_bytes)
    else:
        return extract_text_plain(file_bytes)


# -- Chunking -----------------------------------------------------------------

def chunk_text(text: str, chunk_size: int = 350, overlap: int = 100) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r" {2,}", " ", text)
    header_pattern = re.compile(r'^(#{1,3} .+|[A-Z][A-Z &/\-]{4,})$', re.MULTILINE)
    lines = text.splitlines()
    current_header = ""
    annotated_words = []
    for line in lines:
        stripped = line.strip()
        if header_pattern.match(stripped) and len(stripped) < 80:
            current_header = stripped.lstrip('#').strip()
        for word in stripped.split():
            annotated_words.append((word, current_header))
    chunks = []
    start = 0
    while start < len(annotated_words):
        end = min(start + chunk_size, len(annotated_words))
        segment_words  = [w for w, _ in annotated_words[start:end]]
        segment_header = annotated_words[start][1]
        chunk_body = " ".join(segment_words)
        if len(chunk_body.strip()) > 40:
            if segment_header and not chunk_body.startswith(segment_header):
                chunk = f"[{segment_header}]\n{chunk_body}"
            else:
                chunk = chunk_body
            chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


# -- Supabase storage ---------------------------------------------------------

class DuplicateUpload(Exception):
    """Another request stored the same file a moment ago (unique hash index)."""


def _is_missing_column(err: Exception, column: str) -> bool:
    text = str(err)
    return column in text and ("PGRST204" in text or "does not exist" in text
                               or "Could not find" in text)


def store_document(name: str, file_type: str, size_bytes: int,
                   content_hash: str | None = None) -> int:
    client = get_client()
    row = {"name": name, "file_type": file_type, "size_bytes": size_bytes}
    if content_hash:
        row["content_hash"] = content_hash
    try:
        resp = client.table("documents").insert(row).execute()
    except Exception as e:
        if content_hash and _is_missing_column(e, "content_hash"):
            # Database migration not applied yet: store without the hash.
            log.warning("documents.content_hash missing; storing without it")
            row.pop("content_hash")
            resp = client.table("documents").insert(row).execute()
        elif "23505" in str(e) or "duplicate key" in str(e):
            raise DuplicateUpload() from e
        else:
            raise
    return resp.data[0]["id"]


def find_document_by_hash(content_hash: str) -> dict | None:
    try:
        rows = (get_client().table("documents").select("id, name")
                .eq("content_hash", content_hash).execute().data or [])
    except Exception as e:
        if _is_missing_column(e, "content_hash"):
            return None          # migration not applied yet: no de-duplication
        raise
    return rows[0] if rows else None


def find_documents_by_name(name: str) -> list[dict]:
    return (get_client().table("documents").select("id, name, file_type")
            .eq("name", name).execute().data or [])


def stored_schema(document_ids: list[int]):
    """Column layout of the Data Studio datasets stored for these documents.
    Returns (schema, row counts) keyed by sheet name."""
    client = get_client()
    schema, rows = {}, {}
    for doc_id in document_ids:
        for ds in (client.table("datasets").select("id, sheet_name, row_count")
                   .eq("document_id", doc_id).execute().data or []):
            cols = (client.table("dataset_columns").select("pos, name, detected_type")
                    .eq("dataset_id", ds["id"]).order("pos").execute().data or [])
            schema[ds["sheet_name"] or ""] = [(c["pos"], c["name"], c["detected_type"]) for c in cols]
            rows[ds["sheet_name"] or ""] = ds["row_count"]
    return schema, rows


def store_chunks(document_id: int, document_name: str, chunks: list[str]):
    """
    Insert chunks as text only. Keyword search works as soon as this returns;
    embeddings are added afterwards by embed_pending_chunks().
    """
    client = get_client()
    rows = [
        {
            "document_id":   document_id,
            "document_name": document_name,
            "chunk_index":   i,
            "content":       chunk,
        }
        for i, chunk in enumerate(chunks)
    ]
    for i in range(0, len(rows), CHUNK_INSERT_SIZE):
        client.table("doc_chunks").insert(rows[i:i + CHUNK_INSERT_SIZE]).execute()


def count_pending_chunks(document_id: int) -> int:
    client = get_client()
    resp = (
        client.table("doc_chunks")
        .select("id", count="exact", head=True)
        .eq("document_id", document_id)
        .is_("embedding", "null")
        .execute()
    )
    return resp.count or 0


def embed_pending_chunks(document_id: int, limit_s: float = REQUEST_LIMIT_S) -> dict:
    """
    Embed this document's chunks that have no embedding yet. A new batch only
    starts if even its worst case (all attempts timing out) still finishes
    before limit_s, so the request always ends inside Vercel's 60s limit.
    Safe to call repeatedly; each call picks up where the last one stopped.

    Returns {"embedded": n, "failed": n, "remaining": n} plus "error" when
    the model is permanently unavailable (e.g. retired by NVIDIA).
    "failed" counts chunks whose batch failed even after a retry; they stay
    pending and are retried on the next call.
    """
    started = time.monotonic()
    client  = get_client()
    nim     = _embed_client()
    model    = ai_models.embed_model()
    embedded = failed = 0
    error: str | None = None
    skip_ids: set[int] = set()   # failed this call; don't refetch them in a loop

    while time.monotonic() - started + WORST_BATCH_S < limit_s:
        query = (
            client.table("doc_chunks")
            .select("id, content")
            .eq("document_id", document_id)
            .is_("embedding", "null")
            .order("chunk_index")
            .limit(EMBED_BATCH_SIZE + len(skip_ids))
        )
        rows = [r for r in (query.execute().data or []) if r["id"] not in skip_ids]
        rows = rows[:EMBED_BATCH_SIZE]
        if not rows:
            break

        try:
            vectors = _embed_batch(nim, [r["content"] for r in rows])
        except ModelUnavailableError as e:
            # Retired model / bad key: no point trying more batches.
            log.error("Embedding stopped for document %s: %s", document_id, e)
            error = str(e)
            break
        if vectors is None:
            failed += len(rows)
            skip_ids.update(r["id"] for r in rows)
            continue

        items = [{"id": r["id"], "embedding": v} for r, v in zip(rows, vectors)]
        resp = client.rpc("set_chunk_embeddings", {"items": items, "model": model}).execute()
        embedded += resp.data if isinstance(resp.data, int) else len(items)

    result = {
        "embedded":  embedded,
        "failed":    failed,
        "remaining": count_pending_chunks(document_id),
    }
    if error:
        result["error"] = error
    return result


def delete_document(document_id: int):
    client = get_client()
    client.table("documents").delete().eq("id", document_id).execute()


# -- Main entry point ---------------------------------------------------------

class SchemaChangeNeedsConfirmation(Exception):
    """A same-named spreadsheet has a different column layout. Nothing has
    been stored; the caller shows the changes and asks before replacing."""
    def __init__(self, diff: dict, lines: list[str]):
        super().__init__("; ".join(lines))
        self.diff, self.lines = diff, lines


def process_and_store(filename: str, file_bytes: bytes, file_type: str,
                      replace: bool = False) -> dict:
    """
    Upload pipeline: extract -> chunk -> store text (+ Data Studio rows).

    Idempotent:
      * the same bytes uploaded again (any name) change nothing and return
        {"duplicate": True, ...};
      * a new version under the same name replaces the old one, but only
        after the new one is fully stored. For spreadsheets whose columns
        changed, SchemaChangeNeedsConfirmation is raised first (nothing
        stored) unless replace=True.

    Embedding is NOT done here; the caller drives embed_pending_chunks()
    in follow-up requests so large files can't time out this one.
    Returns {"document_id", "chunks", "name", "data_rows_stored",
             "pending_embeddings", "replaced", "changes"}.
    """
    from services import pipeline_log, schema_diff

    started = pipeline_log.now()
    content_hash = hashlib.sha256(file_bytes).hexdigest()
    details = {"file_type": file_type, "bytes": len(file_bytes), "sha256": content_hash[:16]}

    def skipped(reason: str, **extra):
        pipeline_log.record("upload", filename, "skipped", started,
                            details={**details, "reason": reason, **extra})

    existing = find_document_by_hash(content_hash)
    if existing:
        skipped("duplicate", document_id=existing["id"])
        return _duplicate_result(existing)

    try:
        text = extract_text(file_bytes, file_type)
        if not text.strip():
            raise ValueError("Could not extract any text from the document.")
        chunks = chunk_text(text)
        if not chunks:
            raise ValueError("Document produced no usable text chunks.")

        is_tabular = file_type.lower() in TABULAR_TYPES
        analysed = analyse_tabular(file_bytes, file_type) if is_tabular else None

        previous = find_documents_by_name(filename)
        changes: list[str] = []
        if previous and analysed and not analysed.get("error"):
            old_schema, old_rows = stored_schema([d["id"] for d in previous])
            if old_schema:
                new_schema = {a.sheet_name: [(c.pos, c.name, c.detected_type) for c in a.columns]
                              for _, a in analysed["sheets"]}
                new_rows = {a.sheet_name: a.data_rows for _, a in analysed["sheets"]}
                diff = schema_diff.compare(old_schema, new_schema, old_rows, new_rows)
                changes = schema_diff.summary(diff)
                if diff["changed"] and not replace:
                    skipped("schema_change_unconfirmed", changes=changes)
                    raise SchemaChangeNeedsConfirmation(diff, changes)
                details["schema_diff"] = diff

        try:
            doc_id = store_document(filename, file_type, len(file_bytes), content_hash)
        except DuplicateUpload:
            existing = find_document_by_hash(content_hash)
            skipped("duplicate (concurrent upload)")
            return _duplicate_result(existing or {"id": None, "name": filename})

        # Data Studio: every sheet becomes a dataset, parsed and checked once.
        data_studio = None
        if is_tabular:
            data_studio = import_for_data_studio(doc_id, filename, file_bytes, file_type,
                                                 analysed=analysed)

        try:
            store_chunks(doc_id, filename, chunks)
        except Exception:
            # Don't leave a document row with no chunks behind.
            log.exception("Storing chunks failed for %s; rolling back document %s", filename, doc_id)
            delete_document(doc_id)
            raise

        # The new version is complete: only now remove the old one(s).
        replaced = []
        for d in previous:
            try:
                delete_document(d["id"])
                replaced.append(d["id"])
            except Exception:
                log.exception("Could not remove old version %s of %s", d["id"], filename)

        result = {
            "document_id":        doc_id,
            "chunks":             len(chunks),
            "name":               filename,
            "pending_embeddings": len(chunks),
            "replaced":           replaced,
            "changes":            changes,
        }
        if data_studio is not None:
            result["data_studio"] = data_studio
            result["data_rows_stored"] = sum(d["rows"] for d in data_studio["datasets"])
            if data_studio.get("error"):
                result["data_studio_warning"] = data_studio["error"]
            elif data_studio["warnings"]:
                result["data_studio_warning"] = (
                    f'{data_studio["warnings"]} thing(s) to check in the import report '
                    "(Data Studio → Import check).")

        pipeline_log.record(
            "upload", filename, "success", started,
            rows_in=(analysed or {}).get("rows_in"),
            rows_out=result.get("data_rows_stored"),
            details={**details, "document_id": doc_id, "chunks": len(chunks),
                     "replaced": replaced, "changes": changes,
                     "datasets": (data_studio or {}).get("datasets", [])})
        return result

    except SchemaChangeNeedsConfirmation:
        raise
    except Exception as e:
        pipeline_log.record("upload", filename, "failed", started, details=details,
                            error=f"{type(e).__name__}: {e}")
        raise


def _duplicate_result(existing: dict) -> dict:
    return {
        "duplicate":   True,
        "document_id": existing["id"],
        "name":        existing["name"],
        "message":     f'This exact file is already uploaded as "{existing["name"]}". Nothing was changed.',
    }


TABULAR_TYPES = ("xlsx", "xlsm", "xls", "csv", "tsv")


def analyse_tabular(file_bytes: bytes, file_type: str) -> dict:
    """Parse and analyse every sheet once. Never raises.
    Returns {"sheets": [(RawSheet, Analysed)], "notes": [...], "rows_in": n,
             "error": str | None}."""
    from services import tabular
    out = {"sheets": [], "notes": [], "rows_in": 0, "error": None}
    try:
        sheets, notes = tabular.read_file(file_bytes, file_type)
        out["notes"] = list(notes)
        for sh in sheets:
            out["rows_in"] += len(sh.rows)
            try:
                a = tabular.analyse_sheet(sh)
            except tabular.TabularError as e:
                label = f"Sheet '{sh.name}'" if sh.name else "The file"
                out["notes"].append(f"{label} was not loaded into Data Studio: {e}")
                continue
            if notes:
                a.report["file_notes"] = notes
            out["sheets"].append((sh, a))
    except tabular.TabularError as e:
        out["error"] = str(e)
    except Exception as e:
        log.exception("Reading spreadsheet failed")
        out["error"] = f"Data Studio import failed ({type(e).__name__}). The file is still searchable."
    return out


def import_for_data_studio(doc_id: int, filename: str, file_bytes: bytes, file_type: str,
                           analysed: dict | None = None) -> dict:
    """
    Store each analysed sheet as a Data Studio dataset. Never raises: a file
    Data Studio can't read is still kept for Smart Search, with the reason
    reported to the user.
    Returns {"datasets": [{"id", "sheet", "rows", "warnings"}], "warnings": n,
             "notes": [...], "error": str | None}.
    """
    from services import dataset_store
    if analysed is None:
        analysed = analyse_tabular(file_bytes, file_type)
    out = {"datasets": [], "warnings": 0, "notes": list(analysed["notes"]),
           "error": analysed["error"]}
    if out["error"]:
        return out
    try:
        for sh, a in analysed["sheets"]:
            ds_id = dataset_store.save_dataset(doc_id, filename, a, sh.percent_cols)
            n_warn = sum(1 for w in a.report["warnings"] if w["level"] == "warn")
            out["datasets"].append({"id": ds_id, "sheet": sh.name, "rows": a.data_rows,
                                    "warnings": n_warn})
            out["warnings"] += n_warn
        if not out["datasets"]:
            out["error"] = "No table was found for Data Studio. " + " ".join(out["notes"])
    except Exception as e:
        log.exception("Data Studio import failed for %s", filename)
        out["error"] = f"Data Studio import failed ({type(e).__name__}). The file is still searchable."
    return out


def get_all_documents() -> list[dict]:
    client = get_client()
    docs = (
        client.table("documents")
        .select("*")
        .order("created_at", desc=True)
        .execute()
        .data or []
    )
    # One call for every document's chunk / embedding counts.
    status = {
        row["document_id"]: row
        for row in (client.rpc("document_index_status").execute().data or [])
    }
    for doc in docs:
        s = status.get(doc["id"], {})
        doc["chunk_count"]    = s.get("total_chunks", 0)
        doc["embedded_count"] = s.get("embedded_chunks", 0)
        doc["pending_count"]  = doc["chunk_count"] - doc["embedded_count"]
    return docs
