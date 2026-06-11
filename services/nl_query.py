"""
NL-to-SQL service for the Ops Dashboard.
Converts natural language questions to PostgreSQL SELECT queries,
executes them via Supabase RPC, and returns structured results + AI summary.
"""
import os
import re
from openai import OpenAI
from models.db import get_client

# ── Schema context ──────────────────────────────────────────────────────────

SCHEMA = """
Table: tickets (PostgreSQL)

Columns:
  id              BIGINT        Primary key, auto-increment
  submitter       TEXT          Full name of the person who submitted the ticket
  email           TEXT          Submitter email address
  subject         TEXT          One-line summary of the issue
  description     TEXT          Full description of the issue
  department      TEXT          Routed dept — values: 'IT', 'HR', 'Finance', 'Engineering'
  priority        TEXT          values: 'critical', 'high', 'medium', 'low'
  confidence      REAL          AI confidence score 0.0 to 1.0
  status          TEXT          values: 'pending_review', 'resolved', 'escalated'
  assignee_team   TEXT          Team name handling the ticket
  assignee_email  TEXT          Assignee email
  draft_response  TEXT          AI-generated draft response
  final_response  TEXT          Human-approved final response (NULL if not yet resolved)
  sources         TEXT          JSON array of knowledge base source filenames used
  reasoning       TEXT          AI reasoning for the classification decision
  created_at      TIMESTAMPTZ   When the ticket was created (UTC)
  resolved_at     TIMESTAMPTZ   When resolved (NULL if still open)
"""

SQL_SYSTEM_PROMPT = f"""You are a PostgreSQL expert. Convert natural language questions into valid SELECT SQL queries.

{SCHEMA}

Rules:
- Return ONLY the SQL query — no markdown, no explanation, no code fences.
- Only generate SELECT statements. Never INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE.
- Use exact column and table names as listed above.
- For time filters use: NOW() - INTERVAL '7 days', DATE_TRUNC('day', created_at), etc.
- For grouping + counting use: GROUP BY x ORDER BY count DESC.
- Limit results to 100 rows max unless the user asks for more.
- If the question asks for a "breakdown" or "by X", use GROUP BY and COUNT(*).
- Average resolution time = AVG(EXTRACT(EPOCH FROM (resolved_at - created_at))/3600) as avg_hours.
"""

SUMMARY_SYSTEM_PROMPT = """You are a concise data analyst helping a support operations team.
Given a question, the SQL that answered it, and the result data, write a 2-3 sentence plain-English insight.

Rules:
- Lead with the most important number or finding.
- Call out any anomalies, trends, or actionable observations.
- Be direct. No filler phrases like "Based on the data..." or "It appears that...".
- If the result is empty, say so clearly and suggest why.
- Keep it under 60 words.
"""


def _llm_client() -> OpenAI:
    return OpenAI(
        base_url=os.environ.get("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        api_key=os.environ.get("NVIDIA_API_KEY", ""),
    )


def generate_sql(question: str) -> str:
    """Ask the LLM to produce a SQL query from a natural language question."""
    client = _llm_client()
    response = client.chat.completions.create(
        model=os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct"),
        messages=[
            {"role": "system", "content": SQL_SYSTEM_PROMPT},
            {"role": "user",   "content": question},
        ],
        temperature=0.0,
        max_tokens=512,
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:sql)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def generate_summary(question: str, sql: str, columns: list, rows: list) -> str:
    """Generate a plain-English summary of query results using the LLM."""
    if not rows:
        data_str = "The query returned no rows."
    else:
        # Format top 20 rows as a compact table string
        header = " | ".join(columns)
        lines  = [header, "-" * len(header)]
        for row in rows[:20]:
            lines.append(" | ".join(str(v) if v is not None else "NULL" for v in row))
        if len(rows) > 20:
            lines.append(f"... ({len(rows) - 20} more rows)")
        data_str = "\n".join(lines)

    user_msg = f"""Question: {question}

SQL used:
{sql}

Result:
{data_str}"""

    client = _llm_client()
    response = client.chat.completions.create(
        model=os.environ.get("NVIDIA_MODEL", "meta/llama-3.1-8b-instruct"),
        messages=[
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=120,
    )
    return response.choices[0].message.content.strip()


def is_safe_sql(sql: str) -> bool:
    upper = sql.strip().upper()
    if not upper.startswith("SELECT"):
        return False
    blocked = ["INSERT", "UPDATE", "DELETE", "DROP", "CREATE",
               "ALTER", "TRUNCATE", "GRANT", "REVOKE", "EXECUTE"]
    for kw in blocked:
        if re.search(rf"\b{kw}\b", upper):
            return False
    return True


def execute_query(sql: str) -> dict:
    client = get_client()
    resp = client.rpc("run_query", {"query_text": sql}).execute()
    rows = resp.data or []
    if not rows:
        return {"columns": [], "rows": [], "count": 0}
    columns = list(rows[0].keys())
    return {
        "columns": columns,
        "rows":    [[row.get(c) for c in columns] for row in rows],
        "count":   len(rows),
    }


def nl_query(question: str) -> dict:
    """Full pipeline: question → SQL → execute → summary → structured result."""
    try:
        sql = generate_sql(question)
    except Exception as e:
        return {"error": f"SQL generation failed: {e}", "question": question}

    if not is_safe_sql(sql):
        return {
            "error":    "Safety check failed: generated query is not a plain SELECT.",
            "sql":      sql,
            "question": question,
        }

    try:
        result = execute_query(sql)
    except Exception as e:
        return {"error": str(e), "sql": sql, "question": question}

    # Generate natural language summary
    try:
        result["summary"] = generate_summary(
            question, sql, result["columns"], result["rows"]
        )
    except Exception:
        result["summary"] = None  # non-fatal — UI hides it if missing

    result["sql"]      = sql
    result["question"] = question
    return result
