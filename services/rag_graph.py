"""
RAG pipeline v3 — no LangGraph, no doc_processor dependency.

Pipeline:
  retrieve  -> pgvector semantic search -> FTS fallback -> ILIKE fallback
            -> OR Python aggregation for xlsx/csv
  generate  -> NVIDIA NIM (OpenAI-compatible) with summary memory compression
  parse     -> extract answer + metadata from LLM response
  store     -> persist to Supabase
"""
# v3 - _query_embedding fully inlined, zero cross-module imports
import os
import re
import json
from typing import Optional

from openai import OpenAI

from models.db import get_client


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _llm() -> OpenAI:
    return OpenAI(
        base_url=os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        api_key=os.environ.get("NVIDIA_API_KEY", ""),
    )


def _model() -> str:
    return os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct")


def _query_embedding(text: str):
    """Generate a query-side embedding via NVIDIA NIM. Fully inlined - no external import."""
    try:
        client = OpenAI(
            base_url=os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            api_key=os.environ.get("NVIDIA_API_KEY", ""),
        )
        resp = client.embeddings.create(
            model="nvidia/nv-embedqa-e5-v5",
            input=[text],
            encoding_format="float",
            extra_body={"input_type": "query", "truncate": "END"},
        )
        return resp.data[0].embedding
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STRUCTURED_EXTS = {"xlsx", "xls", "csv"}

LIST_RE = re.compile(
    r"\b(list all|show all|give all|display all|all students|all records|"
    r"enumerate|full list|complete list|every student|each student)\b",
    re.IGNORECASE,
)

_DATA_STOP = {
    "give", "help", "show", "list", "find", "tell", "get", "need", "want",
    "have", "what", "from", "with", "that", "this", "they", "been", "which",
    "percent", "percentage", "who", "are", "the", "you", "already", "uploaded",
    "many", "number", "count", "total", "about", "for", "and", "now", "please",
    "can", "could", "would", "information", "data", "file", "sheet", "excel",
    "document", "dropout", "using", "based",
}

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

AVAILABLE_MODELS = {
    "meta/llama-3.1-8b-instruct": {
        "label":       "Llama 3.1 8B",
        "tag":         "Fast",
        "temperature": 0.2,
        "max_tokens":  900,
        "extra_body":  None,
    },
    "nvidia/nemotron-3-ultra-550b-a55b": {
        "label":       "Nemotron 550B",
        "tag":         "Power",
        "temperature": 0.6,
        "max_tokens":  2048,
        "extra_body":  None,
    },
}

DEFAULT_MODEL = "meta/llama-3.1-8b-instruct"


SYSTEM_PROMPT = """You are a helpful company onboarding assistant. Answer questions accurately using ONLY the provided documentation and context.

Instructions:
- Answer using ONLY the provided context. Do not invent information.
- If context is insufficient, say: "I couldn't find this in the available documentation."
- Be concise. Use bullet points for multi-step processes.
- For structured data (Excel/CSV): always write a full sentence answer (e.g. "There are **14 students** out of 200 total (7%) who match your criteria."). Never output just a raw number. Then add a brief sample of 2-3 matching rows so the user can verify. End with "Would you like me to list all N records?"
- For count/percentage questions: use pre-computed numbers from context -- do not recalculate.
- At the end include:
  OUTDATED_FLAG: YES or NO
  OUTDATED_REASON: (one sentence, only if YES)

Format:
<answer>
Your answer here
</answer>
OUTDATED_FLAG: YES/NO
OUTDATED_REASON: reason if applicable"""


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def _semantic_search(question: str, doc_filter: Optional[str] = None, k: int = 10) -> list:
    """pgvector cosine similarity via match_doc_chunks RPC."""
    emb = _query_embedding(question)
    if emb is None:
        return []
    try:
        params = {"query_embedding": emb, "match_threshold": 0.3, "match_count": k}
        if doc_filter:
            params["doc_name_filter"] = doc_filter
        return get_client().rpc("match_doc_chunks", params).execute().data or []
    except Exception:
        return []


def _fts_fallback(question: str, doc_filter: Optional[str] = None, k: int = 10) -> list:
    """PostgreSQL full-text search fallback."""
    try:
        resp = get_client().rpc("search_docs", {
            "query_text":  question,
            "max_results": k * (3 if doc_filter else 1),
        }).execute()
        rows = resp.data or []
        if doc_filter:
            rows = [r for r in rows if r.get("document_name") == doc_filter]
        return rows[:k]
    except Exception:
        return []


def _ilike_fallback(question: str, doc_filter: Optional[str] = None, k: int = 8) -> list:
    """ILIKE keyword search -- last resort."""
    stop = {"what", "is", "the", "how", "do", "i", "can", "get", "my", "a", "an", "are",
            "to", "for", "of", "in", "on", "at", "and", "or"}
    words = [w for w in re.findall(r"\b[a-zA-Z]\w+\b", question.lower())
             if w not in stop and len(w) > 3]
    seen, results = set(), []
    for word in words[:4]:
        try:
            q = (get_client().table("doc_chunks")
                 .select("id,document_id,document_name,content")
                 .ilike("content", f"%{word}%"))
            if doc_filter:
                q = q.eq("document_name", doc_filter)
            for row in (q.limit(k).execute().data or []):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    results.append(row)
        except Exception:
            continue
        if len(results) >= k:
            break
    return results[:k]


def _retrieve_chunks(question: str, doc_filter: Optional[str]) -> list:
    """Semantic -> FTS -> ILIKE, deduplicated."""
    chunks = _semantic_search(question, doc_filter)
    if not chunks:
        chunks = _fts_fallback(question, doc_filter)
    if not chunks:
        chunks = _ilike_fallback(question, doc_filter)
    seen, merged = set(), []
    for c in chunks:
        if c.get("id") not in seen:
            seen.add(c["id"])
            merged.append(c)
    return merged


_ORDINAL_RE = re.compile(r'\b(\d+)(?:st|nd|rd|th)\b', re.IGNORECASE)

def _normalize_query(text: str) -> str:
    """Normalize ordinals (9th→9), lower-case, keep quoted phrases intact."""
    return _ORDINAL_RE.sub(r'\1', text).lower()

def _python_aggregate(doc_name: str, question: str) -> Optional[str]:
    """Python-level row counting for xlsx/csv -- avoids context overflow."""
    try:
        # Fetch ALL chunks for this document (paginate in batches of 1000)
        all_chunks = []
        offset = 0
        while True:
            resp = (get_client().table("doc_chunks")
                    .select("content")
                    .eq("document_name", doc_name)
                    .range(offset, offset + 999)
                    .execute())
            batch = resp.data or []
            all_chunks.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000

        if not all_chunks:
            return None

        full_text = "\n".join(c.get("content", "") for c in all_chunks)
        data_lines = [
            ln.strip() for ln in full_text.splitlines()
            if ln.strip() and not ln.strip().startswith("##") and not ln.strip().startswith("[")
        ]
        total = len(data_lines)
        if total == 0:
            return None

        # Extract quoted phrases first (e.g. 'Wrong Entry'), then individual words
        q_normalized = _normalize_query(question)
        phrases = re.findall(r"['\"](.+?)['\"]", q_normalized)
        words = [w for w in re.findall(r"\b\w{2,}\b", q_normalized)
                 if w not in _DATA_STOP and not any(w in p for p in phrases)]

        def line_matches_all(ln: str) -> bool:
            ln_l = _normalize_query(ln)
            return (all(p in ln_l for p in phrases) and
                    all(w in ln_l for w in words[:4]))

        def line_matches_any(ln: str) -> bool:
            ln_l = _normalize_query(ln)
            phrase_hit = any(p in ln_l for p in phrases) if phrases else False
            word_hit   = any(w in ln_l for w in words[:4]) if words else False
            return phrase_hit or word_hit

        matched = [ln for ln in data_lines if line_matches_all(ln)]
        if not matched:
            matched = [ln for ln in data_lines if line_matches_any(ln)]

        search_terms = phrases + words[:4]
        pct = round(len(matched) / total * 100, 2) if total > 0 else 0
        wants_list = bool(LIST_RE.search(question))
        summary = (
            f"=== DATA SUMMARY: '{doc_name}' ===\n"
            f"Total records: {total}\n"
            f"Matching records: {len(matched)} ({pct}%)\n"
            f"Search terms: {', '.join(search_terms)}"
        )

        if wants_list or len(matched) <= 10:
            # Show every record — do NOT offer to list again
            rows_text = "\n".join(matched[:60])
            overflow = f"\n[...and {len(matched) - 60} more — ask to see the rest]" if len(matched) > 60 else ""
            label = "ALL MATCHING RECORDS" if len(matched) <= 60 else f"FIRST 60 OF {len(matched)} RECORDS"
            return f"{summary}\n\n=== {label} ===\n{rows_text}{overflow}\n\nDo NOT offer to list records again — they are already shown above."
        else:
            # More than 10 results — show a sample and offer the rest
            sample = "\n".join(matched[:5])
            return (f"{summary}\n\n=== SAMPLE (first 5 of {len(matched)}) ===\n{sample}"
                    f"\n\nTell the user: {len(matched)} records found. Offer to list all of them.")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def _build_messages(history: list, llm: OpenAI) -> list:
    """Convert history to OpenAI message dicts. Histories > 6 turns get summarised."""
    if not history:
        return []

    if len(history) <= 6:
        msgs = []
        for turn in history:
            msgs.append({"role": "user",      "content": turn["question"]})
            msgs.append({"role": "assistant", "content": turn["answer"]})
        return msgs

    old_turns    = history[:-6]
    recent_turns = history[-6:]
    summary_prompt = (
        "Summarise the following conversation in 3-4 sentences, "
        "focusing on what the user was asking about and key facts established:\n\n"
        + "\n".join(f"User: {t['question']}\nAssistant: {t['answer']}" for t in old_turns)
    )
    try:
        r = llm.chat.completions.create(
            model=_model(),
            messages=[{"role": "user", "content": summary_prompt}],
            temperature=0.2,
            max_tokens=200,
        )
        summary = r.choices[0].message.content.strip()
    except Exception:
        summary = f"[Earlier conversation: {len(old_turns)} exchanges]"

    msgs = [{"role": "system", "content": f"Conversation summary: {summary}"}]
    for turn in recent_turns:
        msgs.append({"role": "user",      "content": turn["question"]})
        msgs.append({"role": "assistant", "content": turn["answer"]})
    return msgs


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------

def _parse_response(raw: str) -> dict:
    answer            = raw
    possibly_outdated = False
    outdated_reason   = None

    if "<answer>" in raw and "</answer>" in raw:
        start  = raw.index("<answer>") + len("<answer>")
        end    = raw.rfind("</answer>")
        answer = raw[start:end].strip() if end > start else re.sub(r"</?answer>", "", raw).strip()

    if "OUTDATED_FLAG: YES" in raw.upper():
        possibly_outdated = True

    for line in raw.splitlines():
        if line.upper().startswith("OUTDATED_REASON:"):
            outdated_reason = line.split(":", 1)[1].strip()
            break

    cleaned = [
        ln for ln in answer.splitlines()
        if not ln.strip().upper().startswith("OUTDATED_FLAG:")
        and not ln.strip().upper().startswith("OUTDATED_REASON:")
    ]
    return {
        "answer":            "\n".join(cleaned).strip(),
        "possibly_outdated": possibly_outdated,
        "outdated_reason":   outdated_reason,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_rag(question: str, history: list = None,
            doc_filter: str = None, model: str = None) -> dict:
    """
    Full RAG pipeline: retrieve -> generate -> parse -> store.
    Returns {answer, sources, possibly_outdated, outdated_reason, conversation_id, has_docs}
    """
    history = history or []
    model   = model if model in AVAILABLE_MODELS else DEFAULT_MODEL
    cfg     = AVAILABLE_MODELS[model]

    # 1. Retrieve
    target_structured = (
        doc_filter
        if doc_filter and doc_filter.rsplit(".", 1)[-1].lower() in STRUCTURED_EXTS
        else None
    )

    precomputed = None
    doc_chunks  = []

    if target_structured:
        precomputed = _python_aggregate(target_structured, question)
        doc_chunks  = _retrieve_chunks(question, doc_filter)[:3]
    else:
        doc_chunks = _retrieve_chunks(question, doc_filter)
        struct_doc = next(
            (c["document_name"] for c in doc_chunks
             if c.get("document_name", "").rsplit(".", 1)[-1].lower() in STRUCTURED_EXTS),
            None,
        )
        if struct_doc:
            precomputed = _python_aggregate(struct_doc, question)

    if precomputed:
        context = precomputed
    elif doc_chunks:
        context = "=== DOCUMENTATION ===\n" + "\n\n---\n\n".join(
            f"[{c['document_name']}]\n{c['content']}" for c in doc_chunks
        )
    else:
        context = "No relevant documentation found."

    seen, sources = set(), []
    for c in doc_chunks:
        name = c.get("document_name", "")
        if name not in seen:
            seen.add(name)
            sources.append({"document_name": name,
                            "excerpt": c.get("content", "")[:220] + "..."})

    has_docs = bool(doc_chunks) or bool(precomputed)

    # 2. Generate
    llm      = _llm()
    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + _build_messages(history, llm)
        + [{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}]
    )

    try:
        create_kwargs = dict(
            model=model,
            messages=messages,
            temperature=cfg["temperature"],
            max_tokens=cfg["max_tokens"],
        )
        if cfg["extra_body"]:
            create_kwargs["extra_body"] = cfg["extra_body"]
        resp = llm.chat.completions.create(**create_kwargs)
        raw = resp.choices[0].message.content.strip()
    except Exception as exc:
        raw = f"<answer>Sorry, I encountered an error: {exc}</answer>\nOUTDATED_FLAG: NO"

    # 3. Parse
    parsed = _parse_response(raw)

    # 4. Store
    conv_id = None
    try:
        r = get_client().table("conversations").insert({
            "question":            question,
            "answer":              parsed["answer"],
            "sources":             json.dumps(sources),
            "is_outdated_flagged": parsed["possibly_outdated"],
        }).execute()
        conv_id = r.data[0]["id"] if r.data else None
    except Exception:
        pass

    return {
        "answer":            parsed["answer"],
        "sources":           sources,
        "possibly_outdated": parsed["possibly_outdated"],
        "outdated_reason":   parsed["outdated_reason"],
        "conversation_id":   conv_id,
        "has_docs":          has_docs,
    }
