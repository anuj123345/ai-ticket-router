"""
Document Processor -- parses PDF, DOCX, TXT/MD, XLSX, CSV files,
chunks the text, generates NVIDIA embeddings (pgvector), and stores
in Supabase for semantic + full-text search fallback.
All processing happens in-memory (no disk writes -- Vercel compatible).
"""
from __future__ import annotations
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

def store_document(name: str, file_type: str, size_bytes: int) -> int:
    client = get_client()
    resp = client.table("documents").insert({
        "name":       name,
        "file_type":  file_type,
        "size_bytes": size_bytes,
    }).execute()
    return resp.data[0]["id"]


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


# -- Structured row storage (for Data Studio) ---------------------------------

def _safe_json_val(v):
    """Convert any openpyxl/csv cell value to a JSON-serializable Python primitive."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return None if (v != v) else v  # NaN → None
    if isinstance(v, str):
        s = v.strip()
        return s if s else None
    if hasattr(v, "isoformat"):   # datetime, date, time
        return v.isoformat()
    try:
        import decimal
        if isinstance(v, decimal.Decimal):
            return float(v)
    except (ImportError, OverflowError):
        pass
    return str(v)                 # fallback — never crash


def extract_structured_rows(file_bytes: bytes, file_type: str) -> tuple[list[str], list[dict]]:
    """Extract column headers + raw rows from xlsx/csv for dashboard use."""
    ft = file_type.lower()
    if ft in ("xlsx", "xls"):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
        if not rows:
            return [], []
        headers = [str(h).strip() if h is not None else f"Col{i+1}"
                   for i, h in enumerate(rows[0])]
        data_rows = []
        for row in rows[1:]:
            if all(cell is None or str(cell).strip() == "" for cell in row):
                continue
            row_dict = {}
            for h, v in zip(headers, row):
                safe = _safe_json_val(v)
                if safe is not None:
                    row_dict[h] = safe
            if row_dict:
                data_rows.append(row_dict)
        return headers, data_rows
    elif ft == "csv":
        import csv
        if file_bytes[:2] in (b'\xff\xfe', b'\xfe\xff'):
            text = file_bytes.decode("utf-16", errors="replace")
        elif file_bytes[:3] == b'\xef\xbb\xbf':
            text = file_bytes[3:].decode("utf-8", errors="replace")
        else:
            text = file_bytes.decode("utf-8", errors="replace")
        text = text.replace("\x00", "")
        sample = text[:2000]
        delimiter = "\t" if sample.count("\t") > sample.count(",") else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        headers = list(reader.fieldnames or [])
        data_rows = [dict(r) for r in reader
                     if any(str(v).strip() for v in r.values())]
        return headers, data_rows
    return [], []


MAX_DATA_ROWS = 20000   # safety cap so a huge file can't blow the timeout


def store_structured_rows(document_id: int, document_name: str, rows: list[dict]):
    """Bulk-insert raw structured rows into dataset_rows."""
    client = get_client()
    rows = rows[:MAX_DATA_ROWS]
    batch = []
    for row in rows:
        batch.append({
            "document_id":   document_id,
            "document_name": document_name,
            "row_data":      row,
        })
        if len(batch) >= 500:
            client.table("dataset_rows").insert(batch).execute()
            batch = []
    if batch:
        client.table("dataset_rows").insert(batch).execute()


# -- Main entry point ---------------------------------------------------------

def process_and_store(filename: str, file_bytes: bytes, file_type: str) -> dict:
    """
    Upload pipeline: extract -> chunk -> store text (+ Data Studio rows).
    Embedding is NOT done here; the caller drives embed_pending_chunks()
    in follow-up requests so large files can't time out this one.
    Returns {"document_id", "chunks", "name", "data_rows_stored", "pending_embeddings"}.
    """
    text = extract_text(file_bytes, file_type)
    if not text.strip():
        raise ValueError("Could not extract any text from the document.")

    chunks = chunk_text(text)
    if not chunks:
        raise ValueError("Document produced no usable text chunks.")

    doc_id = store_document(filename, file_type, len(file_bytes))

    # Store raw rows for Data Studio (xlsx/csv only).
    data_studio_warning = None
    data_rows_stored    = 0
    if file_type.lower() in ("xlsx", "xls", "csv"):
        try:
            _, rows = extract_structured_rows(file_bytes, file_type)
            if rows:
                store_structured_rows(doc_id, filename, rows)
                data_rows_stored = min(len(rows), MAX_DATA_ROWS)
                if len(rows) > MAX_DATA_ROWS:
                    data_studio_warning = (
                        f"File has {len(rows):,} rows; only the first "
                        f"{MAX_DATA_ROWS:,} were loaded into Data Studio."
                    )
            else:
                data_studio_warning = "No structured rows found -- file may be empty or have no header row."
        except Exception as e:
            log.exception("Data Studio indexing failed for %s", filename)
            data_studio_warning = f"Data Studio indexing failed: {type(e).__name__}: {e}"

    try:
        store_chunks(doc_id, filename, chunks)
    except Exception:
        # Don't leave a document row with no chunks behind.
        log.exception("Storing chunks failed for %s; rolling back document %s", filename, doc_id)
        delete_document(doc_id)
        raise

    result = {
        "document_id":        doc_id,
        "chunks":             len(chunks),
        "name":               filename,
        "data_rows_stored":   data_rows_stored,
        "pending_embeddings": len(chunks),
    }
    if data_studio_warning:
        result["data_studio_warning"] = data_studio_warning
    return result


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
