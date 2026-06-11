"""
Onboarding Agent — answers employee questions using uploaded company docs.
Features:
  - PostgreSQL full-text search over doc_chunks
  - Verified Q&A pairs (from thumbs-up feedback) included in retrieval
  - Conversation memory (last N exchanges passed as context)
  - Outdated content detection
"""
import os
import json
from openai import OpenAI
from models.db import get_client


SYSTEM_PROMPT = """You are a helpful company onboarding assistant. Answer employee questions accurately using ONLY the provided company documentation and verified Q&A history.

Instructions:
- Answer using ONLY the provided context. Do not invent or assume information.
- If the context doesn't contain enough information, say clearly: "I couldn't find this in the available documentation."
- Be concise and direct. Use bullet points for multi-step processes.
- If prior conversation is provided, use it to understand follow-up questions.
- At the end of your answer include:
  OUTDATED_FLAG: YES or NO
  Mark YES if the content references old dates, says "coming soon", references deprecated systems, or seems vague/inconsistent.
  OUTDATED_REASON: (one sentence, only if YES)

Format:
<answer>
Your answer here
</answer>
OUTDATED_FLAG: YES/NO
OUTDATED_REASON: reason if applicable
"""


def _llm() -> OpenAI:
    return OpenAI(
        base_url=os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        api_key=os.environ.get("NVIDIA_API_KEY", ""),
    )


# ── Retrieval ─────────────────────────────────────────────────────────────────

def search_doc_chunks(question: str, max_results: int = 5) -> list[dict]:
    """Full-text search over uploaded document chunks."""
    client = get_client()
    try:
        resp = client.rpc("search_docs", {
            "query_text":  question,
            "max_results": max_results,
        }).execute()
        return resp.data or []
    except Exception:
        # Fallback to ILIKE
        words = question.split()[:4]
        pattern = "%" + " ".join(words) + "%"
        resp = (
            client.table("doc_chunks")
            .select("id, document_id, document_name, content")
            .ilike("content", pattern)
            .limit(max_results)
            .execute()
        )
        return resp.data or []


def search_verified_qa(question: str, max_results: int = 3) -> list[dict]:
    """Search the verified Q&A table built from thumbs-up feedback."""
    client = get_client()
    try:
        sql = (
            "SELECT question, answer, upvotes FROM verified_qa "
            "WHERE search_vector @@ websearch_to_tsquery('english', "
            + repr(question) +
            ") ORDER BY upvotes DESC LIMIT " + str(max_results)
        )
        resp = client.rpc("run_query", {"query_text": sql}).execute()
        return resp.data or []
    except Exception:
        return []


def build_context(doc_chunks: list[dict], verified_qa: list[dict]) -> str:
    parts = []

    if verified_qa:
        parts.append("=== VERIFIED ANSWERS FROM PREVIOUS QUESTIONS ===")
        for qa in verified_qa:
            parts.append(
                f"Q: {qa['question']}\nA: {qa['answer']}"
            )

    if doc_chunks:
        parts.append("=== COMPANY DOCUMENTATION ===")
        for c in doc_chunks:
            parts.append(f"[{c['document_name']}]\n{c['content']}")

    return "\n\n---\n\n".join(parts) if parts else "No relevant documentation found."


# ── Response parsing ──────────────────────────────────────────────────────────

def parse_response(raw: str) -> dict:
    answer = raw
    possibly_outdated = False
    outdated_reason = None

    if "<answer>" in raw and "</answer>" in raw:
        start = raw.index("<answer>") + len("<answer>")
        end   = raw.index("</answer>")
        answer = raw[start:end].strip()

    if "OUTDATED_FLAG: YES" in raw.upper():
        possibly_outdated = True

    for line in raw.splitlines():
        if line.upper().startswith("OUTDATED_REASON:"):
            outdated_reason = line.split(":", 1)[1].strip()
            break

    return {
        "answer":            answer,
        "possibly_outdated": possibly_outdated,
        "outdated_reason":   outdated_reason,
    }


# ── Main pipeline ─────────────────────────────────────────────────────────────

def answer_question(question: str, history: list[dict] | None = None) -> dict:
    """
    Full pipeline: retrieve → generate → parse → store.
    history: list of {"question": str, "answer": str} from recent session turns.
    Returns: {answer, sources, possibly_outdated, outdated_reason, conversation_id, has_docs}
    """
    # 1. Retrieve context
    doc_chunks  = search_doc_chunks(question)
    verified_qa = search_verified_qa(question)
    context     = build_context(doc_chunks, verified_qa)

    sources = []
    seen_names = set()
    for c in doc_chunks:
        if c["document_name"] not in seen_names:
            seen_names.add(c["document_name"])
            sources.append({
                "document_name": c["document_name"],
                "excerpt":       c["content"][:220] + "…",
            })

    # 2. Build messages with conversation memory
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if history:
        for turn in history[-5:]:           # keep last 5 exchanges
            messages.append({"role": "user",      "content": turn["question"]})
            messages.append({"role": "assistant", "content": turn["answer"]})

    user_msg = f"Company documentation & verified answers:\n{context}\n\nEmployee question: {question}"
    messages.append({"role": "user", "content": user_msg})

    # 3. Generate
    llm = _llm()
    response = llm.chat.completions.create(
        model=os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct"),
        messages=messages,
        temperature=0.2,
        max_tokens=900,
    )
    raw    = response.choices[0].message.content.strip()
    parsed = parse_response(raw)

    # 4. Save conversation
    db = get_client()
    conv_resp = db.table("conversations").insert({
        "question":            question,
        "answer":              parsed["answer"],
        "sources":             json.dumps(sources),
        "is_outdated_flagged": parsed["possibly_outdated"],
    }).execute()
    conversation_id = conv_resp.data[0]["id"] if conv_resp.data else None

    return {
        "answer":            parsed["answer"],
        "sources":           sources,
        "possibly_outdated": parsed["possibly_outdated"],
        "outdated_reason":   parsed["outdated_reason"],
        "conversation_id":   conversation_id,
        "has_docs":          len(doc_chunks) > 0 or len(verified_qa) > 0,
    }


def save_verified_qa(conversation_id: int):
    """
    Called when a user gives thumbs up.
    Saves the Q&A pair to verified_qa, or increments upvotes if similar exists.
    """
    db = get_client()
    conv = db.table("conversations").select("question, answer").eq("id", conversation_id).execute()
    if not conv.data:
        return

    row = conv.data[0]
    q, a = row["question"], row["answer"]

    # Check if this exact question already exists
    existing = db.table("verified_qa").select("id, upvotes").eq("question", q).execute()
    if existing.data:
        db.table("verified_qa").update({
            "upvotes": existing.data[0]["upvotes"] + 1
        }).eq("id", existing.data[0]["id"]).execute()
    else:
        db.table("verified_qa").insert({"question": q, "answer": a}).execute()


def get_feedback_stats() -> dict:
    db = get_client()
    feedback = db.table("feedback").select("rating, is_flagged").execute().data or []
    total    = len(feedback)
    positive = sum(1 for f in feedback if f.get("rating") ==  1)
    negative = sum(1 for f in feedback if f.get("rating") == -1)
    flagged  = sum(1 for f in feedback if f.get("is_flagged"))
    qa_count = db.table("verified_qa").select("id", count="exact").execute().count or 0
    return {
        "total":    total,
        "positive": positive,
        "negative": negative,
        "flagged":  flagged,
        "qa_count": qa_count,
    }
