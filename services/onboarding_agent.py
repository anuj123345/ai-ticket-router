"""
Onboarding Agent — answers employee questions using uploaded company docs.
Features:
  - PostgreSQL full-text search over doc_chunks
  - Synonym expansion before search (handles PTO/vacation, remote/WFH, etc.)
  - Multi-query retrieval — runs original + expanded queries, merges results
  - Verified Q&A pairs (from thumbs-up feedback) included in retrieval
  - Conversation memory (last N exchanges passed as context)
  - Outdated content detection
"""
import os
import re
import json
from openai import OpenAI
from models.db import get_client


SYSTEM_PROMPT = """You are a helpful company onboarding assistant. Answer employee questions accurately using ONLY the provided company documentation and verified Q&A history.

Instructions:
- Answer using ONLY the provided context. Do not invent or assume information.
- If the context doesn't contain enough information, say clearly: "I couldn't find this in the available documentation."
- Be concise and direct. Use bullet points for multi-step processes.
- If prior conversation is provided, use it to understand follow-up questions.
- For analytical questions (count, percentage, total, how many, breakdown): carefully read ALL the provided data rows, count/aggregate them yourself, and give a precise numerical answer. Do not say you can't calculate — use the data rows provided.
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


# ── Synonym dictionary ────────────────────────────────────────────────────────
# Maps query terms → canonical doc terms to bridge semantic gaps in FTS.
# Keys are patterns (lowercased), values are replacement/addition terms.

SYNONYM_MAP = [
    # PTO / leave
    (r'\bpto\b',                         'vacation'),
    (r'\btime off\b',                    'vacation leave'),
    (r'\bdays off\b',                    'vacation days'),
    (r'\bpaid leave\b',                  'vacation sick leave'),
    (r'\bannual leave\b',                'vacation'),
    # Sick leave
    (r'\bsick day(s)?\b',               'sick leave'),
    (r'\billness\b',                     'sick leave'),
    # Remote / WFH
    (r'\bwfh\b',                         'remote work'),
    (r'\bwork from home\b',              'remote work'),
    (r'\btelecommut\w+\b',               'remote work'),
    (r'\bhybrid work\b',                 'remote work'),
    # Parental leave
    (r'\bmaternal(ity)?\b',              'parental leave'),
    (r'\bpaternal(ity)?\b',              'parental leave'),
    (r'\bbaby leave\b',                  'parental leave'),
    (r'\bnewborn\b',                     'parental leave'),
    (r'\badoption leave\b',              'parental leave'),
    # Retirement / 401k
    (r'\bretirement\b',                  '401k'),
    (r'\bpension\b',                     '401k'),
    (r'\bsavings plan\b',                '401k'),
    (r'\bcompany match\b',               '401k'),
    # Health insurance
    (r'\bmedical(ly)?\b',                'health insurance'),
    (r'\bhealthcare\b',                  'health insurance'),
    (r'\bmedical coverage\b',            'health insurance'),
    (r'\bhealth (benefit|plan|cover)\b', 'health insurance'),
    (r'\bdental\b',                      'health insurance dental'),
    (r'\bvision (coverage|plan|benefit)',  'health insurance vision'),
    # Expenses
    (r'\breimburse(ment)?\b',            'expense reimbursement'),
    (r'\bspend\b',                       'expense reimbursement'),
    (r'\breceipt\b',                     'expense reimbursement'),
    # Performance / review
    (r'\bperformance review\b',          'performance review promotion'),
    (r'\bpromotion\b',                   'performance review promotion'),
    (r'\bsalary review\b',               'performance review'),
    (r'\bpay rise\b',                    'performance review salary'),
    (r'\braise\b',                       'performance review salary'),
    # Probation
    (r'\btrial period\b',                'probation'),
    (r'\bprobationary\b',                'probation'),
    # IT / laptop
    (r'\blaptop setup\b',                'laptop setup IT'),
    (r'\bgetting started\b',             'laptop setup access'),
    (r'\bnew employee setup\b',          'laptop access IT'),
    # Learning & development
    (r'\btraining budget\b',             'learning development L&D'),
    (r'\bl&d\b',                         'learning development'),
    (r'\bcertification\b',               'learning development'),
    (r'\bcourse(s)?\b',                  'learning development'),
    # Referral
    (r'\bemployee referral\b',           'referral bonus'),
    (r'\brefer a friend\b',              'referral bonus'),
    # Mental health / EAP
    (r'\bmental health\b',               'mental health EAP therapy'),
    (r'\btherapy\b',                     'mental health EAP'),
    (r'\bcounseling\b',                  'mental health EAP'),
    # Discipline / termination
    (r'\bfired\b',                       'termination'),
    (r'\bdismissal\b',                   'termination disciplinary'),
    (r'\bwarning\b',                     'disciplinary warning'),
    (r'\bpip\b',                         'performance improvement plan'),
    # VPN / security
    (r'\bvpn\b',                         'VPN remote access'),
    (r'\bpassword\b',                    'security password'),
    (r'\b2fa\b',                         'two-factor authentication'),
]


def expand_query(query: str) -> str:
    """Apply synonym expansion to bridge FTS lexeme gaps."""
    expanded = query.lower()
    additions = []
    for pattern, replacement in SYNONYM_MAP:
        if re.search(pattern, expanded):
            additions.append(replacement)
    if additions:
        return query + ' ' + ' '.join(additions)
    return query


def extract_key_terms(query: str) -> str:
    """Pull nouns/keywords for a focused second-pass search."""
    stop = {'what', 'is', 'the', 'how', 'do', 'i', 'can', 'get', 'my', 'a',
            'an', 'are', 'was', 'were', 'will', 'to', 'for', 'of', 'in',
            'on', 'at', 'with', 'and', 'or', 'about', 'me', 'we', 'our',
            'us', 'have', 'has', 'does', 'did', 'be', 'been', 'there',
            'when', 'where', 'which', 'who', 'that', 'this', 'if', 'from'}
    words = re.findall(r'\b[a-zA-Z]\w+\b', query.lower())
    key = [w for w in words if w not in stop and len(w) > 2]
    return ' '.join(key[:6])  # top 6 content words


def _llm() -> OpenAI:
    return OpenAI(
        base_url=os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        api_key=os.environ.get("NVIDIA_API_KEY", ""),
    )


# ── Retrieval ─────────────────────────────────────────────────────────────────

def _fts_search(query_text: str, max_results: int) -> list[dict]:
    """Run a single FTS query via search_docs RPC."""
    client = get_client()
    try:
        resp = client.rpc("search_docs", {
            "query_text":  query_text,
            "max_results": max_results,
        }).execute()
        return resp.data or []
    except Exception:
        return []


def _ilike_fallback(query: str, max_results: int) -> list[dict]:
    """Keyword-level ILIKE search — tries each significant word separately."""
    client = get_client()
    stop = {'what', 'is', 'the', 'how', 'do', 'i', 'can', 'get', 'my', 'a',
            'an', 'are', 'to', 'for', 'of', 'in', 'on', 'at', 'and', 'or'}
    words = [w for w in re.findall(r'\b[a-zA-Z]\w+\b', query.lower())
             if w not in stop and len(w) > 3]

    seen_ids = set()
    results = []
    for word in words[:4]:
        try:
            resp = (
                client.table("doc_chunks")
                .select("id, document_id, document_name, content")
                .ilike("content", f"%{word}%")
                .limit(max_results)
                .execute()
            )
            for row in (resp.data or []):
                if row['id'] not in seen_ids:
                    seen_ids.add(row['id'])
                    results.append(row)
        except Exception:
            continue
        if len(results) >= max_results:
            break
    return results[:max_results]


ANALYTICAL_PATTERNS = re.compile(
    r'\b(percent|percentage|how many|count|total|average|sum|ratio|breakdown|'
    r'proportion|number of|tally|aggregate|calculate|statistic)\b',
    re.IGNORECASE
)

STRUCTURED_EXTS = {'xlsx', 'xls', 'csv'}


def _is_analytical(question: str) -> bool:
    return bool(ANALYTICAL_PATTERNS.search(question))


def _get_all_chunks_for_docs(doc_names: list[str]) -> list[dict]:
    """Retrieve every chunk from the specified documents (for analytical queries)."""
    client = get_client()
    results = []
    seen_ids = set()
    for name in doc_names:
        try:
            resp = (
                client.table("doc_chunks")
                .select("id, document_id, document_name, content, chunk_index")
                .eq("document_name", name)
                .order("chunk_index")
                .limit(500)
                .execute()
            )
            for row in (resp.data or []):
                if row['id'] not in seen_ids:
                    seen_ids.add(row['id'])
                    results.append(row)
        except Exception:
            continue
    return results


def search_doc_chunks(question: str, max_results: int = 5) -> list[dict]:
    """
    Multi-query retrieval:
    1. Original question  → FTS
    2. Synonym-expanded   → FTS
    3. Key terms only     → FTS
    4. ILIKE fallback on failure
    For analytical queries on structured files (xlsx/csv), fetches ALL chunks
    from the matched document so the LLM can count/aggregate correctly.
    Deduplicates by chunk ID, returns up to max_results.
    """
    # Three query variants
    q_original = question
    q_expanded = expand_query(question)
    q_keywords = extract_key_terms(question)

    seen_ids = set()
    merged = []

    def add_results(rows):
        for row in rows:
            if row.get('id') not in seen_ids:
                seen_ids.add(row['id'])
                merged.append(row)

    # Pass 1: original query
    add_results(_fts_search(q_original, max_results))

    # Pass 2: synonym-expanded (only if different from original)
    if q_expanded != q_original:
        add_results(_fts_search(q_expanded, max_results))

    # Pass 3: key terms only (catches cases where full phrase confuses FTS)
    if q_keywords and q_keywords != q_original.lower():
        add_results(_fts_search(q_keywords, max_results))

    # Fallback to ILIKE if FTS returned nothing
    if not merged:
        add_results(_ilike_fallback(question, max_results))

    # For analytical questions, pull ALL chunks from any structured file matched
    if _is_analytical(question) and merged:
        structured_docs = list({
            c['document_name'] for c in merged
            if c.get('document_name', '').rsplit('.', 1)[-1].lower() in STRUCTURED_EXTS
        })
        if structured_docs:
            all_chunks = _get_all_chunks_for_docs(structured_docs)
            if all_chunks:
                # Replace merged with full dataset so LLM can aggregate
                seen_ids = {c['id'] for c in all_chunks}
                non_structured = [c for c in merged if c['id'] not in seen_ids]
                return all_chunks + non_structured

    return merged[:max_results * 2]  # return more chunks = more LLM context


def search_verified_qa(question: str, max_results: int = 3) -> list[dict]:
    """Search the verified Q&A table built from thumbs-up feedback."""
    client = get_client()
    try:
        # Try both original and expanded query
        q_exp = expand_query(question)
        search_term = q_exp if q_exp != question else question

        sql = (
            "SELECT question, answer, upvotes FROM verified_qa "
            "WHERE search_vector @@ websearch_to_tsquery('english', "
            + repr(search_term) +
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
            parts.append(f"Q: {qa['question']}\nA: {qa['answer']}")

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

    # Strip any leaked OUTDATED_FLAG / OUTDATED_REASON lines from the answer
    cleaned = []
    for line in answer.splitlines():
        upper = line.strip().upper()
        if upper.startswith("OUTDATED_FLAG:") or upper.startswith("OUTDATED_reason:".upper()):
            continue
        cleaned.append(line)
    answer = "\n".join(cleaned).strip()

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
        for turn in history[-5:]:
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
    """Called on thumbs up — saves Q&A to verified_qa or increments upvotes."""
    db = get_client()
    conv = db.table("conversations").select("question, answer").eq("id", conversation_id).execute()
    if not conv.data:
        return

    row = conv.data[0]
    q, a = row["question"], row["answer"]

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
