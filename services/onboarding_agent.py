"""
Onboarding Agent — answers employee questions using uploaded company docs.
Uses PostgreSQL full-text search (Supabase) for retrieval,
NVIDIA NIM for answer generation.
Returns: answer, cited sources, possibly_outdated flag.
"""
import os
import json
from openai import OpenAI
from models.db import get_client


SYSTEM_PROMPT = """You are a helpful company onboarding assistant. Your job is to answer employee questions accurately based ONLY on the provided company documentation excerpts.

Instructions:
- Answer using ONLY the provided context. Do not invent or assume information.
- If the context doesn't contain enough information, say clearly: "I couldn't find this in the available documentation."
- Be concise and direct. Use bullet points for multi-step processes.
- At the end of your answer, add a line: OUTDATED_FLAG: YES or OUTDATED_FLAG: NO
  Mark YES if: the content references old dates, says things like "coming soon", references deprecated systems, or seems inconsistent/vague.
  Mark NO otherwise.

Format your response as:
<answer>
Your answer here
</answer>
OUTDATED_FLAG: YES/NO
OUTDATED_REASON: (only if YES — one sentence explaining why)
"""


def _llm_client() -> OpenAI:
    return OpenAI(
        base_url=os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        api_key=os.environ.get("NVIDIA_API_KEY", ""),
    )


def search_docs(question: str, max_results: int = 6) -> list[dict]:
    """Retrieve relevant doc chunks via PostgreSQL full-text search."""
    client = get_client()
    try:
        resp = client.rpc("search_docs", {
            "query_text":  question,
            "max_results": max_results,
        }).execute()
        return resp.data or []
    except Exception:
        # Fallback: ILIKE search if full-text fails (e.g. single-word query)
        words = question.split()[:5]
        pattern = "%" + "%".join(words) + "%"
        resp = (
            client.table("doc_chunks")
            .select("id, document_id, document_name, content")
            .ilike("content", pattern)
            .limit(max_results)
            .execute()
        )
        return resp.data or []


def format_context(chunks: list[dict]) -> str:
    if not chunks:
        return "No relevant documentation found."
    parts = []
    for i, c in enumerate(chunks, 1):
        parts.append(f"[Source {i}: {c['document_name']}]\n{c['content']}")
    return "\n\n---\n\n".join(parts)


def parse_llm_response(raw: str) -> dict:
    """Extract answer, outdated flag, and reason from LLM output."""
    answer = raw
    possibly_outdated = False
    outdated_reason = None

    # Extract <answer> block
    if "<answer>" in raw and "</answer>" in raw:
        start = raw.index("<answer>") + len("<answer>")
        end   = raw.index("</answer>")
        answer = raw[start:end].strip()

    # Extract OUTDATED_FLAG
    if "OUTDATED_FLAG: YES" in raw.upper():
        possibly_outdated = True
    
    # Extract OUTDATED_REASON
    for line in raw.splitlines():
        if line.upper().startswith("OUTDATED_REASON:"):
            outdated_reason = line.split(":", 1)[1].strip()
            break

    return {
        "answer":            answer,
        "possibly_outdated": possibly_outdated,
        "outdated_reason":   outdated_reason,
    }


def answer_question(question: str) -> dict:
    """
    Full pipeline: retrieve → generate → parse.
    Returns {answer, sources, possibly_outdated, outdated_reason, conversation_id}
    """
    # 1. Retrieve relevant chunks
    chunks = search_docs(question)
    context = format_context(chunks)

    sources = [
        {"document_name": c["document_name"], "excerpt": c["content"][:200] + "…"}
        for c in chunks
    ]
    # Deduplicate sources by document name
    seen, unique_sources = set(), []
    for s in sources:
        if s["document_name"] not in seen:
            seen.add(s["document_name"])
            unique_sources.append(s)

    # 2. Generate answer
    llm = _llm_client()
    user_msg = f"""Company documentation:
{context}

Employee question: {question}"""

    response = llm.chat.completions.create(
        model=os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct"),
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.2,
        max_tokens=800,
    )
    raw = response.choices[0].message.content.strip()
    parsed = parse_llm_response(raw)

    # 3. Save conversation to Supabase
    db = get_client()
    conv_resp = db.table("conversations").insert({
        "question":            question,
        "answer":              parsed["answer"],
        "sources":             json.dumps(unique_sources),
        "is_outdated_flagged": parsed["possibly_outdated"],
    }).execute()
    conversation_id = conv_resp.data[0]["id"] if conv_resp.data else None

    return {
        "answer":            parsed["answer"],
        "sources":           unique_sources,
        "possibly_outdated": parsed["possibly_outdated"],
        "outdated_reason":   parsed["outdated_reason"],
        "conversation_id":   conversation_id,
        "has_docs":          len(chunks) > 0,
    }


def get_feedback_stats() -> dict:
    """Return feedback summary for the admin panel."""
    db = get_client()
    feedback = db.table("feedback").select("rating, is_flagged").execute().data or []
    total     = len(feedback)
    positive  = sum(1 for f in feedback if f.get("rating") == 1)
    negative  = sum(1 for f in feedback if f.get("rating") == -1)
    flagged   = sum(1 for f in feedback if f.get("is_flagged"))
    return {
        "total":    total,
        "positive": positive,
        "negative": negative,
        "flagged":  flagged,
    }
