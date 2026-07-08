"""
RAG Graph — LangGraph-powered retrieval + generation pipeline.

Graph nodes:
  retrieve  → semantic vector search (pgvector) with FTS fallback
            → OR Python aggregation for xlsx/csv analytical queries
  generate  → LangChain ChatOpenAI (NVIDIA NIM) with summary memory
  store     → persist conversation to Supabase

Memory: ConversationSummaryBufferMemory compresses history > 6 turns
into a running summary so the LLM retains long context without token overflow.
"""
import os
import re
import json
from typing import TypedDict, Optional

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END

from models.db import get_client
from services.doc_processor import query_embedding


# ── LLM ──────────────────────────────────────────────────────────────────────

def _llm() -> ChatOpenAI:
    return ChatOpenAI(
        base_url=os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        api_key=os.environ.get("NVIDIA_API_KEY", ""),
        model=os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct"),
        temperature=0.2,
        max_tokens=900,
    )


# ── Graph state ───────────────────────────────────────────────────────────────

class RAGState(TypedDict):
    question:          str
    doc_filter:        Optional[str]
    history:           list[dict]          # [{question, answer}, ...]
    doc_chunks:        list[dict]
    precomputed:       Optional[str]       # Python-aggregated result for xlsx/csv
    context:           str
    messages:          list                # LangChain message objects
    raw_answer:        str
    answer:            str
    sources:           list[dict]
    possibly_outdated: bool
    outdated_reason:   Optional[str]
    conversation_id:   Optional[int]
    has_docs:          bool


# ── Constants ─────────────────────────────────────────────────────────────────

STRUCTURED_EXTS = {"xlsx", "xls", "csv"}

ANALYTICAL_RE = re.compile(
    r"\b(percent|percentage|how many|count|total|average|sum|ratio|breakdown|"
    r"proportion|number of|tally|aggregate|calculate|statistic)\b",
    re.IGNORECASE,
)

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

SYSTEM_PROMPT = """You are a helpful company onboarding assistant. Answer questions accurately using ONLY the provided documentation and context.

Instructions:
- Answer using ONLY the provided context. Do not invent information.
- If context is insufficient, say: "I couldn't find this in the available documentation."
- Be concise. Use bullet points for multi-step processes.
- For structured data (Excel/CSV): lead with the key stat (e.g. "Found 14 of 200 — 7%"), then a brief sample. Do NOT dump the full list unless a FULL MATCHING RECORDS section is provided. End with "Would you like me to list all N records?"
- For count/percentage questions: use pre-computed numbers from context — do not recalculate.
- At the end include:
  OUTDATED_FLAG: YES or NO
  OUTDATED_REASON: (one sentence, only if YES)

Format:
<answer>
Your answer here
</answer>
OUTDATED_FLAG: YES/NO
OUTDATED_REASON: reason if applicable"""


# ── Retrieval helpers ─────────────────────────────────────────────────────────

def _semantic_search(question: str, doc_filter: Optional[str] = None,
                     k: int = 10) -> list[dict]:
    """pgvector cosine similarity search via match_doc_chunks RPC."""
    emb = query_embedding(question)
    if emb is None:
        return []
    client = get_client()
    try:
        params = {
            "query_embedding": emb,
            "match_threshold": 0.3,
            "match_count": k,
        }
        if doc_filter:
            params["doc_name_filter"] = doc_filter
        resp = client.rpc("match_doc_chunks", params).execute()
        return resp.data or []
    except Exception:
        return []


def _fts_fallback(question: str, doc_filter: Optional[str] = None,
                  k: int = 10) -> list[dict]:
    """PostgreSQL full-text search fallback for chunks without embeddings."""
    client = get_client()
    try:
        resp = client.rpc("search_docs", {
            "query_text":  question,
            "max_results": k * (3 if doc_filter else 1),
        }).execute()
        rows = resp.data or []
        if doc_filter:
            rows = [r for r in rows if r.get("document_name") == doc_filter]
        return rows[:k]
    except Exception:
        return []


def _ilike_fallback(question: str, doc_filter: Optional[str] = None,
                    k: int = 8) -> list[dict]:
    """ILIKE keyword search — last resort."""
    client = get_client()
    stop = {"what","is","the","how","do","i","can","get","my","a","an","are","to","for","of","in","on","at","and","or"}
    words = [w for w in re.findall(r"\b[a-zA-Z]\w+\b", question.lower())
             if w not in stop and len(w) > 3]
    seen, results = set(), []
    for word in words[:4]:
        try:
            q = client.table("doc_chunks").select("id,document_id,document_name,content").ilike("content", f"%{word}%")
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


def _retrieve_chunks(question: str, doc_filter: Optional[str]) -> list[dict]:
    """Semantic search → FTS fallback → ILIKE fallback."""
    chunks = _semantic_search(question, doc_filter)
    if not chunks:
        chunks = _fts_fallback(question, doc_filter)
    if not chunks:
        chunks = _ilike_fallback(question, doc_filter)
    # Deduplicate by id
    seen, merged = set(), []
    for c in chunks:
        if c.get("id") not in seen:
            seen.add(c["id"])
            merged.append(c)
    return merged


def _python_aggregate(doc_name: str, question: str) -> Optional[str]:
    """Python-level row counting/filtering for xlsx/csv — avoids context overflow."""
    client = get_client()
    try:
        resp = (
            client.table("doc_chunks")
            .select("content")
            .eq("document_name", doc_name)
            .limit(500)
            .execute()
        )
        if not resp.data:
            return None
        full_text = "\n".join(c.get("content", "") for c in resp.data)
        data_lines = [
            ln.strip() for ln in full_text.splitlines()
            if ln.strip() and not ln.strip().startswith("##") and not ln.strip().startswith("[")
        ]
        total = len(data_lines)
        if total == 0:
            return None

        words = [w.lower() for w in re.findall(r"\b\w{3,}\b", question.lower())
                 if w.lower() not in _DATA_STOP]

        matched = [ln for ln in data_lines if all(w in ln.lower() for w in words[:5])]
        if not matched:
            matched = [ln for ln in data_lines if any(w in ln.lower() for w in words[:5])]

        pct = round(len(matched) / total * 100, 2) if total > 0 else 0
        wants_list = bool(LIST_RE.search(question))

        summary = (
            f"=== DATA SUMMARY: '{doc_name}' ===\n"
            f"Total records: {total}\n"
            f"Matching records: {len(matched)} ({pct}%)\n"
            f"Search terms: {', '.join(words[:5])}"
        )

        if wants_list:
            rows_text = "\n".join(matched[:60])
            overflow = f"\n[...and {len(matched)-60} more]" if len(matched) > 60 else ""
            return f"{summary}\n\n=== FULL MATCHING RECORDS ===\n{rows_text}{overflow}"
        else:
            sample = "\n".join(matched[:5])
            offer = f"\n\nTell the user: {len(matched)} records found. Offer the full list." if len(matched) > 5 else ""
            return f"{summary}\n\n=== SAMPLE (first 5 of {len(matched)}) ===\n{sample}{offer}"
    except Exception:
        return None


# ── Memory helper ─────────────────────────────────────────────────────────────

def _build_memory_messages(history: list[dict], llm: ChatOpenAI) -> list:
    """
    Convert history to LangChain messages.
    If history > 6 turns, summarise older turns with the LLM to compress context.
    """
    if not history:
        return []

    if len(history) <= 6:
        msgs = []
        for turn in history:
            msgs.append(HumanMessage(content=turn["question"]))
            msgs.append(AIMessage(content=turn["answer"]))
        return msgs

    # Summarise older turns
    old_turns = history[:-6]
    recent_turns = history[-6:]

    summary_prompt = (
        "Summarise the following conversation in 3-4 sentences, "
        "focusing on what the user was asking about and key facts established:\n\n"
        + "\n".join(f"User: {t['question']}\nAssistant: {t['answer']}" for t in old_turns)
    )
    try:
        summary_resp = llm.invoke([HumanMessage(content=summary_prompt)])
        summary_text = summary_resp.content
    except Exception:
        summary_text = f"[Earlier conversation covered {len(old_turns)} exchanges]"

    msgs = [SystemMessage(content=f"Conversation summary so far: {summary_text}")]
    for turn in recent_turns:
        msgs.append(HumanMessage(content=turn["question"]))
        msgs.append(AIMessage(content=turn["answer"]))
    return msgs


# ── Graph nodes ───────────────────────────────────────────────────────────────

def retrieve_node(state: RAGState) -> RAGState:
    """Retrieve relevant context — semantic search or structured aggregation."""
    question   = state["question"]
    doc_filter = state.get("doc_filter")

    # Determine if structured file is targeted
    target_structured = (
        doc_filter if doc_filter and doc_filter.rsplit(".", 1)[-1].lower() in STRUCTURED_EXTS
        else None
    )

    precomputed = None
    doc_chunks = []

    if target_structured:
        # Always use Python aggregation for structured files — never raw chunks
        precomputed = _python_aggregate(target_structured, question)
        # Still get a couple chunks for the sources panel
        doc_chunks = _retrieve_chunks(question, doc_filter)[:3]
    else:
        doc_chunks = _retrieve_chunks(question, doc_filter)
        # Check if any result is a structured file
        struct_doc = next(
            (c["document_name"] for c in doc_chunks
             if c.get("document_name", "").rsplit(".", 1)[-1].lower() in STRUCTURED_EXTS),
            None,
        )
        if struct_doc:
            precomputed = _python_aggregate(struct_doc, question)

    # Build context string
    if precomputed:
        context = precomputed
    elif doc_chunks:
        context = "=== DOCUMENTATION ===\n" + "\n\n---\n\n".join(
            f"[{c['document_name']}]\n{c['content']}" for c in doc_chunks
        )
    else:
        context = "No relevant documentation found."

    # Build sources list (deduplicated by doc name)
    seen, sources = set(), []
    for c in doc_chunks:
        name = c.get("document_name", "")
        if name not in seen:
            seen.add(name)
            sources.append({"document_name": name, "excerpt": c.get("content", "")[:220] + "…"})

    return {
        **state,
        "doc_chunks":  doc_chunks,
        "precomputed": precomputed,
        "context":     context,
        "sources":     sources,
        "has_docs":    bool(doc_chunks) or bool(precomputed),
    }


def generate_node(state: RAGState) -> RAGState:
    """Generate answer using LangChain ChatOpenAI with summary memory."""
    llm = _llm()

    # Build message list: system + compressed history + current question
    memory_msgs = _build_memory_messages(state.get("history", []), llm)

    messages = (
        [SystemMessage(content=SYSTEM_PROMPT)]
        + memory_msgs
        + [HumanMessage(content=f"Context:\n{state['context']}\n\nQuestion: {state['question']}")]
    )

    try:
        resp = llm.invoke(messages)
        raw = resp.content.strip()
    except Exception as e:
        raw = f"<answer>Sorry, I encountered an error: {e}</answer>\nOUTDATED_FLAG: NO"

    return {**state, "raw_answer": raw, "messages": messages}


def parse_node(state: RAGState) -> RAGState:
    """Parse the raw LLM response into answer + metadata."""
    raw = state["raw_answer"]
    answer = raw
    possibly_outdated = False
    outdated_reason = None

    if "<answer>" in raw and "</answer>" in raw:
        start = raw.index("<answer>") + len("<answer>")
        end   = raw.rfind("</answer>")
        if end > start:
            answer = raw[start:end].strip()
        else:
            answer = re.sub(r"</?answer>", "", raw).strip()

    if "OUTDATED_FLAG: YES" in raw.upper():
        possibly_outdated = True

    for line in raw.splitlines():
        if line.upper().startswith("OUTDATED_REASON:"):
            outdated_reason = line.split(":", 1)[1].strip()
            break

    # Strip leaked flag lines
    cleaned = [
        ln for ln in answer.splitlines()
        if not ln.strip().upper().startswith("OUTDATED_FLAG:")
        and not ln.strip().upper().startswith("OUTDATED_REASON:")
    ]
    answer = "\n".join(cleaned).strip()

    return {
        **state,
        "answer":            answer,
        "possibly_outdated": possibly_outdated,
        "outdated_reason":   outdated_reason,
    }


def store_node(state: RAGState) -> RAGState:
    """Persist the conversation turn to Supabase."""
    db = get_client()
    try:
        resp = db.table("conversations").insert({
            "question":            state["question"],
            "answer":              state["answer"],
            "sources":             json.dumps(state["sources"]),
            "is_outdated_flagged": state["possibly_outdated"],
        }).execute()
        conv_id = resp.data[0]["id"] if resp.data else None
    except Exception:
        conv_id = None

    return {**state, "conversation_id": conv_id}


# ── Build graph ───────────────────────────────────────────────────────────────

def build_rag_graph():
    graph = StateGraph(RAGState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("generate", generate_node)
    graph.add_node("parse",    parse_node)
    graph.add_node("store",    store_node)

    graph.set_entry_point("retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", "parse")
    graph.add_edge("parse",    "store")
    graph.add_edge("store",    END)

    return graph.compile()


# Singleton — compiled once at import time
_graph = None

def get_graph():
    global _graph
    if _graph is None:
        _graph = build_rag_graph()
    return _graph


def run_rag(question: str, history: list[dict] | None = None,
            doc_filter: str | None = None) -> dict:
    """
    Main entry point. Runs the LangGraph pipeline and returns the result dict.
    """
    initial_state: RAGState = {
        "question":          question,
        "doc_filter":        doc_filter,
        "history":           history or [],
        "doc_chunks":        [],
        "precomputed":       None,
        "context":           "",
        "messages":          [],
        "raw_answer":        "",
        "answer":            "",
        "sources":           [],
        "possibly_outdated": False,
        "outdated_reason":   None,
        "conversation_id":   None,
        "has_docs":          False,
    }
    result = get_graph().invoke(initial_state)
    return {
        "answer":            result["answer"],
        "sources":           result["sources"],
        "possibly_outdated": result["possibly_outdated"],
        "outdated_reason":   result["outdated_reason"],
        "conversation_id":   result["conversation_id"],
        "has_docs":          result["has_docs"],
    }
