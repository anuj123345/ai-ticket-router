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
import json
import logging
import os
import re
from typing import Optional

from openai import OpenAI

from models.db import get_client
from services import ai_models

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _llm() -> OpenAI:
    return OpenAI(
        base_url=os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        api_key=os.environ.get("NVIDIA_API_KEY", ""),
    )


def _model() -> str:
    return ai_models.chat_model()


def _query_embedding(text: str):
    """Query-side embedding, or None so retrieval falls back to keyword search."""
    return ai_models.query_embedding(text)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STRUCTURED_EXTS = {"xlsx", "xls", "csv"}

# ---------------------------------------------------------------------------
# Model registry
# ---------------------------------------------------------------------------

# Tiers the chat UI offers. The UI sends the tier key ("standard"/"power");
# the actual model IDs live only here and in ai_models, so a model retirement
# is a one-line change (or an env var change) instead of a UI edit.
POWER_MODEL = ai_models.power_model()   # NVIDIA_POWER_MODEL env var

AVAILABLE_MODELS = {
    "standard": {
        "model":       ai_models.chat_model(),       # NVIDIA_MODEL env var
        "label":       "Standard",
        "tag":         "Fast",
        "temperature": 0.2,
        "max_tokens":  900,
        "extra_body":  None,
    },
    "power": {
        "model":       POWER_MODEL,
        "label":       "Power",
        "tag":         "Deeper",
        "temperature": 0.6,
        "max_tokens":  2048,
        "extra_body":  None,
    },
}

DEFAULT_MODEL = "standard"


def resolve_model_tier(requested: str | None) -> str:
    """Map whatever the client sent (tier key, or a raw model ID saved in an
    old browser session) to a tier key. Unknown or retired IDs -> default."""
    if requested in AVAILABLE_MODELS:
        return requested
    for tier, cfg in AVAILABLE_MODELS.items():
        if cfg["model"] == requested:
            return tier
    return DEFAULT_MODEL


SYSTEM_PROMPT = """You are a helpful company onboarding assistant. Answer questions accurately using ONLY the provided documentation and context.

Instructions:
- Answer using ONLY the provided context. Do not invent information.
- If context is insufficient, say: "I couldn't find this in the available documentation."
- Be concise. Use bullet points for multi-step processes.
- Spreadsheet (Excel/CSV) excerpts are only a small sample of rows. NEVER count, add up,
  average or rank rows from them, and never state a number of rows. If the question needs
  such a number, say: "I couldn't calculate this exactly. Try asking with a column name,
  for example: How many rows have Status = Active?"
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

def _normalize_doc_filter(doc_filter):
    """
    Normalise doc_filter to one of:
      None          → search all documents
      str           → restrict to a single document
      set[str]      → restrict to multiple documents
    Accepts str, list, set, or None as input.
    """
    if not doc_filter:
        return None
    if isinstance(doc_filter, str):
        return doc_filter
    docs = set(doc_filter) if not isinstance(doc_filter, set) else doc_filter
    if len(docs) == 0:
        return None
    if len(docs) == 1:
        return next(iter(docs))
    return docs


def _semantic_search(question: str, doc_filter=None, k: int = 10) -> list:
    """pgvector cosine similarity via match_doc_chunks RPC."""
    emb = _query_embedding(question)
    if emb is None:
        return []
    # For multi-doc we skip RPC-level filter and post-filter in Python
    is_multi = isinstance(doc_filter, set)
    try:
        params = {
            "query_embedding": emb,
            "match_threshold": 0.3,
            "match_count": k * (max(2, len(doc_filter)) if is_multi else 1),
        }
        if isinstance(doc_filter, str):
            params["doc_name_filter"] = doc_filter
        rows = get_client().rpc("match_doc_chunks", params).execute().data or []
        if is_multi:
            rows = [r for r in rows if r.get("document_name") in doc_filter]
        return rows[:k]
    except Exception:
        return []


def _fts_fallback(question: str, doc_filter=None, k: int = 10) -> list:
    """PostgreSQL full-text search fallback."""
    is_multi = isinstance(doc_filter, set)
    try:
        resp = get_client().rpc("search_docs", {
            "query_text":  question,
            "max_results": k * (max(3, len(doc_filter)) if is_multi else (3 if doc_filter else 1)),
        }).execute()
        rows = resp.data or []
        if isinstance(doc_filter, str):
            rows = [r for r in rows if r.get("document_name") == doc_filter]
        elif is_multi:
            rows = [r for r in rows if r.get("document_name") in doc_filter]
        return rows[:k]
    except Exception:
        return []


def _ilike_fallback(question: str, doc_filter=None, k: int = 8) -> list:
    """ILIKE keyword search -- last resort."""
    stop = {"what", "is", "the", "how", "do", "i", "can", "get", "my", "a", "an", "are",
            "to", "for", "of", "in", "on", "at", "and", "or"}
    words = [w for w in re.findall(r"\b[a-zA-Z]\w+\b", question.lower())
             if w not in stop and len(w) > 3]
    is_multi = isinstance(doc_filter, set)
    seen, results = set(), []
    for word in words[:4]:
        try:
            q = (get_client().table("doc_chunks")
                 .select("id,document_id,document_name,content")
                 .ilike("content", f"%{word}%"))
            if isinstance(doc_filter, str):
                q = q.eq("document_name", doc_filter)
            elif is_multi:
                q = q.in_("document_name", list(doc_filter))
            for row in (q.limit(k).execute().data or []):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    results.append(row)
        except Exception:
            continue
        if len(results) >= k:
            break
    return results[:k]


def _retrieve_chunks(question: str, doc_filter) -> list:
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
        summary = ai_models.chat_completion(
            [{"role": "user", "content": summary_prompt}],
            temperature=0.2,
            max_tokens=200,
            nim=llm,
        )
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
            doc_filter=None, model: str = None) -> dict:
    """
    Full RAG pipeline: retrieve -> generate -> parse -> store.
    doc_filter: None (all), str (single doc), list/set (multiple docs).
    Returns {answer, sources, possibly_outdated, outdated_reason, conversation_id, has_docs}
    """
    history    = history or []
    cfg        = AVAILABLE_MODELS[resolve_model_tier(model)]
    model      = cfg["model"]
    doc_filter = _normalize_doc_filter(doc_filter)

    # 1. Retrieve
    # Determine if we're targeting one or more structured (CSV/XLSX) docs
    if isinstance(doc_filter, str):
        target_structured = (
            doc_filter
            if doc_filter.rsplit(".", 1)[-1].lower() in STRUCTURED_EXTS
            else None
        )
    elif isinstance(doc_filter, set):
        # All structured docs in the selection
        target_structured = [d for d in doc_filter
                              if d.rsplit(".", 1)[-1].lower() in STRUCTURED_EXTS] or None
    else:
        target_structured = None

    # Spreadsheet questions are answered by the exact engine (data_answer),
    # never by the model reading text excerpts.
    from services import data_answer
    structured = ([target_structured] if isinstance(target_structured, str)
                  else list(target_structured or []))
    doc_chunks = []
    if not structured:
        doc_chunks = _retrieve_chunks(question, doc_filter)
        hit = next((c["document_name"] for c in doc_chunks
                    if c.get("document_name", "").rsplit(".", 1)[-1].lower() in STRUCTURED_EXTS), None)
        if hit:
            structured = [hit]
    if structured:
        try:
            exact = data_answer.answer(question, structured)
        except Exception:
            log.exception("Exact data answer failed; falling back to document search")
            exact = None
        if exact:
            return _finish(question, exact["answer"], exact["sources"], False, None, True)
        if not doc_chunks:
            doc_chunks = _retrieve_chunks(question, doc_filter)[:3]

    if doc_chunks:
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

    has_docs = bool(doc_chunks)

    # 2. Generate
    llm      = _llm()
    messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + _build_messages(history, llm)
        + [{"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}]
    )

    try:
        raw = ai_models.chat_completion(
            messages,
            model=model,
            temperature=cfg["temperature"],
            max_tokens=cfg["max_tokens"],
            nim=llm,
        )
    except Exception as exc:
        log.error("Smart Search answer failed (%s): %s", model, exc)
        raw = ("<answer>Sorry, I couldn't generate an answer right now. "
               "Please try again in a moment.</answer>\nOUTDATED_FLAG: NO")

    # 3. Parse
    parsed = _parse_response(raw)

    return _finish(question, parsed["answer"], sources, parsed["possibly_outdated"],
                   parsed["outdated_reason"], has_docs)


def _finish(question: str, answer: str, sources: list, outdated: bool,
            outdated_reason, has_docs: bool) -> dict:
    """Store the exchange and shape the reply."""
    conv_id = None
    try:
        r = get_client().table("conversations").insert({
            "question":            question,
            "answer":              answer,
            "sources":             json.dumps(sources),
            "is_outdated_flagged": outdated,
        }).execute()
        conv_id = r.data[0]["id"] if r.data else None
    except Exception:
        log.exception("Could not store the conversation")
    return {
        "answer":            answer,
        "sources":           sources,
        "possibly_outdated": outdated,
        "outdated_reason":   outdated_reason,
        "conversation_id":   conv_id,
        "has_docs":          has_docs,
    }
