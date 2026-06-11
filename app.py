"""
AI Ticket Router — Flask Application
Entry point. Run with: python app.py
"""

import json
import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from dotenv import load_dotenv

from models.db import init_db, create_ticket, get_ticket, get_all_tickets, update_ticket_status, get_stats
from services.classifier import classify_ticket, get_assignee
from services.response_gen import generate_draft, format_full_response
from services.nl_query import nl_query as run_nl_query

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__,
    static_folder=os.path.join(BASE_DIR, "static"),
    template_folder=os.path.join(BASE_DIR, "templates")
)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")

# Initialize database on startup
init_db()


# ─── Ticket Routes ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Ticket submission form."""
    return render_template("index.html")


@app.route("/submit", methods=["POST"])
def submit_ticket():
    subject     = request.form.get("subject",     "").strip()
    description = request.form.get("description", "").strip()
    submitter   = request.form.get("submitter",   "").strip()
    email       = request.form.get("email",       "").strip()

    if not all([subject, description, submitter, email]):
        flash("All fields are required.", "error")
        return redirect(url_for("index"))

    try:
        classification = classify_ticket(subject, description)
        department     = classification["department"]
        assignee       = get_assignee(department)
        result         = generate_draft(subject, description, department, submitter)

        ticket_id = create_ticket({
            "subject":        subject,
            "description":    description,
            "submitter":      submitter,
            "email":          email,
            "department":     department,
            "priority":       classification["priority"],
            "confidence":     classification["confidence"],
            "reasoning":      classification["reasoning"],
            "assignee_team":  assignee["team"],
            "assignee_email": assignee["email"],
            "assignee_slack": assignee["slack"],
            "draft_response": result["draft"],
            "sources":        json.dumps([s["source"] for s in result["sources"]]),
            "status":         "pending_review",
        })

        return redirect(url_for("review_ticket", ticket_id=ticket_id))

    except Exception as e:
        flash(f"Error processing ticket: {str(e)}", "error")
        return redirect(url_for("index"))


@app.route("/review/<int:ticket_id>")
def review_ticket(ticket_id: int):
    ticket = get_ticket(ticket_id)
    if not ticket:
        flash("Ticket not found.", "error")
        return redirect(url_for("dashboard"))

    sources = []
    if ticket.get("sources"):
        try:
            sources = json.loads(ticket["sources"])
        except Exception:
            pass

    return render_template("review.html", ticket=ticket, sources=sources)


@app.route("/approve/<int:ticket_id>", methods=["POST"])
def approve_ticket(ticket_id: int):
    ticket = get_ticket(ticket_id)
    if not ticket:
        flash("Ticket not found.", "error")
        return redirect(url_for("dashboard"))

    edited_response = request.form.get("final_response", "").strip()
    if not edited_response:
        flash("Response cannot be empty.", "error")
        return redirect(url_for("review_ticket", ticket_id=ticket_id))

    final = format_full_response(
        draft=edited_response,
        submitter_name=ticket["submitter"],
        agent_team=ticket["assignee_team"],
    )

    update_ticket_status(ticket_id, status="resolved", final_response=final)
    flash(f"Ticket #{ticket_id} approved and response sent to {ticket['email']}.", "success")
    return redirect(url_for("dashboard"))


@app.route("/reject/<int:ticket_id>", methods=["POST"])
def reject_ticket(ticket_id: int):
    update_ticket_status(ticket_id, status="escalated")
    flash(f"Ticket #{ticket_id} escalated for manual review.", "warning")
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    tickets = get_all_tickets(limit=100)
    stats   = get_stats()
    return render_template("dashboard.html", tickets=tickets, stats=stats)


@app.route("/api/stats")
def api_stats():
    return jsonify(get_stats())


@app.route("/ticket/<int:ticket_id>")
def ticket_detail(ticket_id: int):
    ticket = get_ticket(ticket_id)
    if not ticket:
        flash("Ticket not found.", "error")
        return redirect(url_for("dashboard"))
    sources = []
    if ticket.get("sources"):
        try:
            sources = json.loads(ticket["sources"])
        except Exception:
            pass
    return render_template("review.html", ticket=ticket, sources=sources, readonly=True)


# ─── Ops Dashboard Routes ─────────────────────────────────────────────────────

@app.route("/ops")
def ops_dashboard():
    """AI Ops Dashboard — natural language analytics over ticket data."""
    return render_template("ops_dashboard.html")


@app.route("/ops/query", methods=["POST"])
def ops_query():
    """Execute a natural language query and return JSON results."""
    data     = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "Question is required."}), 400
    result = run_nl_query(question)
    return jsonify(result)


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Starting AI Ticket Router...")
    print("Open http://localhost:5000 in your browser.")
    app.run(debug=True, port=5000)
