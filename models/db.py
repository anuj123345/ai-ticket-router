"""
Database layer — Supabase (PostgreSQL via supabase-py v2).
Replaces SQLite; data persists across Vercel cold starts.
"""
import os
from datetime import datetime, timezone
from supabase import create_client, Client




_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        _client = create_client(url, key)
    return _client


def init_db():
    """No-op: schema is managed via Supabase dashboard."""
    pass


def create_ticket(data: dict) -> int:
    """Insert a new ticket and return its ID."""
    client = get_client()
    response = client.table("tickets").insert(data).execute()
    return response.data[0]["id"]


def get_ticket(ticket_id: int) -> dict | None:
    client = get_client()
    response = (
        client.table("tickets")
        .select("*")
        .eq("id", ticket_id)
        .execute()
    )
    return response.data[0] if response.data else None


def get_all_tickets(limit: int = 100) -> list[dict]:
    client = get_client()
    response = (
        client.table("tickets")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return response.data or []


def update_ticket_status(ticket_id: int, status: str, final_response: str = None):
    """Update ticket status; optionally save the approved final response."""
    client = get_client()
    payload = {"status": status}
    if final_response:
        payload["final_response"] = final_response
        payload["resolved_at"] = datetime.now(timezone.utc).isoformat()
    client.table("tickets").update(payload).eq("id", ticket_id).execute()


def get_stats() -> dict:
    """Return summary stats for the dashboard."""
    client = get_client()
    rows = (
        client.table("tickets")
        .select("department, priority, status")
        .execute()
        .data or []
    )

    total = len(rows)
    by_dept: dict = {}
    by_priority: dict = {}
    by_status: dict = {}

    for r in rows:
        dept = r.get("department") or "Unknown"
        pri  = r.get("priority")   or "Unknown"
        stat = r.get("status")     or "Unknown"
        by_dept[dept]         = by_dept.get(dept, 0) + 1
        by_priority[pri]      = by_priority.get(pri, 0) + 1
        by_status[stat]       = by_status.get(stat, 0) + 1

    return {
        "total":         total,
        "by_department": by_dept,
        "by_priority":   by_priority,
        "by_status":     by_status,
    }
