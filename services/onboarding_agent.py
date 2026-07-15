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
from __future__ import annotations
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
- For structured data (Excel/CSV): ALWAYS lead with a one-line summary stat (e.g. "Found 14 matching records out of 200 total — 7%"). Then show a brief sample if helpful. Do NOT dump the full list unless the pre-computed data explicitly includes a FULL MATCHING RECORDS section. If more records exist, end with "Would you like me to list all N records?"
- For analytical questions (count, percentage, total, how many, breakdown): use the pre-computed summary provided — do not recalculate.
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

def _fts_search(query_text: str, max_results: int, doc_filter: str | None = None) -> list[dict]:
    """Run a single FTS query via search_docs RPC, then optionally filter by doc name."""
    client = get_client()
    try:
        resp = client.rpc("search_docs", {
            "query_text":  query_text,
            "max_results": max_results if not doc_filter else max_results * 3,
        }).execute()
        rows = resp.data or []
        if doc_filter:
            rows = [r for r in rows if r.get("document_name") == doc_filter]
        return rows
    except Exception:
        return []


def _ilike_fallback(query: str, max_results: int, doc_filter: str | None = None) -> list[dict]:
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
            q = (
                client.table("doc_chunks")
                .select("id, document_id, document_name, content")
                .ilike("content", f"%{word}%")
            )
            if doc_filter:
                q = q.eq("document_name", doc_filter)
            resp = q.limit(max_results).execute()
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

LIST_PATTERNS = re.compile(
    r'\b(list all|show all|give all|display all|all students|all records|'
    r'enumerate|full list|complete list|every student|each student)\b',
    re.IGNORECASE
)

STRUCTURED_EXTS = {'xlsx', 'xls', 'csv'}

# Words to strip from the question before extracting data search terms
_DATA_STOP = {
    'give', 'help', 'show', 'list', 'find', 'tell', 'get', 'need', 'want',
    'have', 'what', 'from', 'with', 'that', 'this', 'they', 'been', 'which',
    'percent', 'percentage', 'who', 'are', 'the', 'you', 'already', 'uploaded',
    'many', 'number', 'count', 'total', 'about', 'for', 'and', 'now', 'please',
    'can', 'could', 'would', 'information', 'data', 'file', 'sheet', 'excel',
    'document', 'dropout', 'using', 'based',
}


def _is_analytical(question: str) -> bool:
    return bool(ANALYTICAL_PATTERNS.search(question))


def _query_structured_doc(doc_name: str, question: str) -> str | None:
    """
    For any query targeting an xlsx/csv doc:
    - Fetch ALL chunk content from the document
    - Extract individual data rows in Python
    - Filter rows that match the question's key terms
    - Return a compact result (count + matching rows) instead of raw chunks
    This avoids both context overflow AND missing data due to top-N retrieval limits.
    """
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

        # Each Excel row becomes one line; skip ## headers and [section] labels
        data_lines = [
            ln.strip() for ln in full_text.splitlines()
            if ln.strip()
            and not ln.strip().startswith("##")
            and not ln.strip().startswith("[")
        ]
        total = len(data_lines)
        if total == 0:
            return None

        # Extract meaningful data-search terms from the question
        words = [
            w.lower() for w in re.findall(r'\b\w{3,}\b', question.lower())
            if w.lower() not in _DATA_STOP
        ]

        # Strict match first (all terms in the same line)
        matched = [ln for ln in data_lines if all(w in ln.lower() for w in words[:5])]
        if not matched:
            # Broad match (any term)
            matched = [ln for ln in data_lines if any(w in ln.lower() for w in words[:5])]

        match_count = len(matched)
        pct = round(match_count / total * 100, 2) if total > 0 else 0

        wants_full_list = bool(LIST_PATTERNS.search(question))

        # Always lead with analytics summary
        summary = (
            f"=== DATA SUMMARY: '{doc_name}' ===\n"
            f"Total records in file: {total}\n"
            f"Matching records: {match_count} ({pct}%)\n"
            f"Search terms used: {', '.join(words[:5])}"
        )

        if wants_full_list:
            # User explicitly asked for the full list
            rows_text = "\n".join(matched[:60])
            overflow = f"\n[...and {match_count - 60} more records not shown]" if match_count > 60 else ""
            return (
                f"{summary}\n\n"
                f"=== FULL MATCHING RECORDS ===\n"
                f"{rows_text}{overflow}"
            )
        else:
            # Show analytics + a small sample, offer the full list
            sample = "\n".join(matched[:5])
            offer = f"\n\nTell the user: {match_count} matching records found. Ask if they want the full list." if match_count > 5 else ""
            return (
                f"{summary}\n\n"
                f"=== SAMPLE (first 5 of {match_count}) ===\n"
                f"{sample}"
                f"{offer}"
            )
    except Exception:
        return None


def search_doc_chunks(question: str, max_results: int = 5,
                      doc_filter: str | None = None) -> list[dict]:
    """
    Multi-query retrieval:
    1. Original question  → FTS
    2. Synonym-expanded   → FTS
    3. Key terms only     → FTS
    4. ILIKE fallback on failure
    When doc_filter is set, results are restricted to that document only.
    Deduplicates by chunk ID, returns up to max_results.
    """
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

    add_results(_fts_search(q_original, max_results, doc_filter))
    if q_expanded != q_original:
        add_results(_fts_search(q_expanded, max_results, doc_filter))
    if q_keywords and q_keywords != q_original.lower():
        add_results(_fts_search(q_keywords, max_results, doc_filter))
    if not merged:
        add_results(_ilike_fallback(question, max_results, doc_filter))

    return merged[:max_results * 2]


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
        # Use rfind for </answer> so we capture the last (complete) block
        start = raw.index("<answer>") + len("<answer>")
        end   = raw.rfind("</answer>")
        if end > start:
            answer = raw[start:end].strip()
        else:
            # Malformed tags — strip all XML-like tags from raw
            answer = re.sub(r'</?answer>', '', raw).strip()

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

def answer_question(question: str, history: list[dict] | None = None,
                    doc_filter: str | None = None, model: str | None = None) -> dict:
    """
    Delegates to the LangGraph RAG pipeline in services/rag_graph.py.
    history: list of {"question": str, "answer": str} from recent session turns.
    doc_filter: restrict retrieval to a single document name (or None = all docs).
    model: NVIDIA NIM model string (falls back to DEFAULT_MODEL if invalid/None).
    Returns: {answer, sources, possibly_outdated, outdated_reason, conversation_id, has_docs}
    """
    from services.rag_graph import run_rag
    return run_rag(question, history=history, doc_filter=doc_filter, model=model)


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


def suggest_questions(doc_names: str | list[str], n: int = 6) -> list[str]:
    """
    Generate n suggested questions for one or more documents.
    For multiple docs, includes cross-document comparison questions where relevant.
    """
    # Normalise to list
    if isinstance(doc_names, str):
        doc_names = [doc_names]
    doc_names = [d for d in doc_names if d]
    if not doc_names:
        return []

    client = get_client()
    try:
        if len(doc_names) == 1:
            # ── Single doc (original logic) ──────────────────────────────
            doc_name = doc_names[0]
            resp = (
                client.table("doc_chunks")
                .select("content")
                .eq("document_name", doc_name)
                .limit(8)
                .execute()
            )
            chunks = resp.data or []
            if not chunks:
                return []

            sample = "\n\n".join(c["content"] for c in chunks[:5])
            ext = doc_name.rsplit(".", 1)[-1].lower()

            if ext in STRUCTURED_EXTS:
                prompt = (
                    f"You are given a sample of structured data from '{doc_name}':\n\n"
                    f"{sample}\n\n"
                    f"Generate exactly {n} short, specific questions a user might ask about this data. "
                    f"Focus on counts, percentages, filtering, and lookups. "
                    f"Return ONLY a numbered list, one question per line, no explanation."
                )
            else:
                prompt = (
                    f"You are given a sample from the document '{doc_name}':\n\n"
                    f"{sample}\n\n"
                    f"Generate exactly {n} short questions an employee might ask about this document. "
                    f"Make them specific and practical. "
                    f"Return ONLY a numbered list, one question per line, no explanation."
                )

        else:
            # ── Multiple docs — generate cross-doc questions ──────────────
            samples = []
            has_structured = False
            for doc in doc_names[:4]:        # cap at 4 to keep context short
                resp = (
                    client.table("doc_chunks")
                    .select("content")
                    .eq("document_name", doc)
                    .limit(4)
                    .execute()
                )
                chunks = resp.data or []
                if chunks:
                    preview = "\n".join(c["content"] for c in chunks[:3])[:600]
                    samples.append(f"### {doc}\n{preview}")
                if doc.rsplit(".", 1)[-1].lower() in STRUCTURED_EXTS:
                    has_structured = True

            if not samples:
                return []

            docs_label  = ", ".join(f'"{d}"' for d in doc_names)
            cross_hint  = (
                "Include some questions that compare or combine data across the files."
                if len(doc_names) > 1 else ""
            )
            data_hint   = (
                "Focus on counts, percentages, filtering, lookups, and comparisons."
                if has_structured
                else "Make them specific and practical."
            )

            prompt = (
                f"The user has selected these documents: {docs_label}.\n\n"
                f"Sample content from each:\n\n"
                + "\n\n".join(samples)
                + f"\n\nGenerate exactly {n} short, specific questions a user might ask across these documents. "
                f"{cross_hint} {data_hint} "
                f"Return ONLY a numbered list, one question per line, no explanation."
            )

        llm = _llm()
        resp = llm.chat.completions.create(
            model=os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=400,
        )
        raw = resp.choices[0].message.content.strip()

        questions = []
        for line in raw.splitlines():
            line = re.sub(r'^\d+[\.\)]\s*', '', line).strip()
            if line and len(line) > 10:
                questions.append(line)
        return questions[:n]

    except Exception:
        return []


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
