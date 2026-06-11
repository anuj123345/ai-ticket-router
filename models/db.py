"""
Database models — uses SQLite via Python's built-in sqlite3.
No ORM needed; keeps the project dependency-light.
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "tickets.db")


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # rows behave like dicts
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            subject     TEXT NOT NULL,
            description TEXT NOT NULL,
            submitter   TEXT NOT NULL,
            email       TEXT NOT NULL,
            department  TEXT,
            priority    TEXT,
            confidence  REAL,
            reasoning   TEXT,
            assignee_team   TEXT,
            assignee_email  TEXT,
            assignee_slack  TEXT,
            draft_response  TEXT,
            final_response  TEXT,
            sources     TEXT,
            status      TEXT DEFAULT 'pending_review',
            created_at  TEXT DEFAULT (datetime('now')),
            resolved_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def create_ticket(data: dict) -> int:
    """Insert a new ticket and return its ID."""
    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO tickets (
            subject, description, submitter, email,
            department, priority, confidence, reasoning,
            assignee_team, assignee_email, assignee_slack,
            draft_response, sources, status
        ) VALUES (
            :subject, :description, :submitter, :email,
            :department, :priority, :confidence, :reasoning,
            :assignee_team, :assignee_email, :assignee_slack,
            :draft_response, :sources, :status
        )
    """, data)
    conn.commit()
    ticket_id = cursor.lastrowid
    conn.close()
    return ticket_id


def get_ticket(ticket_id: int) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_tickets(limit: int = 50) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM tickets ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_ticket_status(ticket_id: int, status: str, final_response: str = None):
    """Update ticket status and optionally set the final approved response."""
    conn = get_connection()
    if final_response:
        conn.execute(
            """UPDATE tickets
               SET status = ?, final_response = ?, resolved_at = datetime('now')
               WHERE id = ?""",
            (status, final_response, ticket_id),
        )
    else:
        conn.execute(
            "UPDATE tickets SET status = ? WHERE id = ?",
            (status, ticket_id),
        )
    conn.commit()
    conn.close()


def get_stats() -> dict:
    """Return summary stats for the dashboard."""
    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    by_dept = conn.execute(
        "SELECT department, COUNT(*) as count FROM tickets GROUP BY department"
    ).fetchall()
    by_priority = conn.execute(
        "SELECT priority, COUNT(*) as count FROM tickets GROUP BY priority"
    ).fetchall()
    by_status = conn.execute(
        "SELECT status, COUNT(*) as count FROM tickets GROUP BY status"
    ).fetchall()
    conn.close()

    return {
        "total": total,
        "by_department": {r["department"]: r["count"] for r in by_dept},
        "by_priority": {r["priority"]: r["count"] for r in by_priority},
        "by_status": {r["status"]: r["count"] for r in by_status},
    }
