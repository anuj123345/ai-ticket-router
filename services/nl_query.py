"""
NL-to-SQL service for the Ops Dashboard.
Converts natural language questions to PostgreSQL SELECT queries,
executes them via Supabase RPC, and returns structured results.
"""
import os
import re
from openai import OpenAI
from models.db import get_client

# ── Schema context fed to the LLM ──────────────────────────────────────────

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

SYSTEM_PROMPT = f"""You are a PostgreSQL expert. Convert natural language questions into valid SELECT SQL queries.

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
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": question},
        ],
        temperature=0.0,
        max_tokens=512,
    )
    raw = response.choices[0].message.content.strip()
    # Strip markdown fences if the model adds them
    raw = re.sub(r"^```(?:sql)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def is_safe_sql(sql: str) -> bool:
    """Reject anything that isn't a plain SELECT."""
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
    """Run the SQL via Supabase RPC and return columns + rows."""
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
    """Full pipeline: question → SQL → execute → structured result."""
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
        result["sql"]      = sql
        result["question"] = question
        return result
    except Exception as e:
        return {"error": str(e), "sql": sql, "question": question}
