"""
AI Ticket Router — Flask Application
"""

import json
import os
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, flash, jsonify, session)
from dotenv import load_dotenv

from models.db import init_db, create_ticket, get_ticket, get_all_tickets, update_ticket_status, get_stats
from services.classifier import classify_ticket, get_assignee
from services.response_gen import generate_draft, format_full_response
from services.nl_query import nl_query as run_nl_query
from services.doc_processor import process_and_store, get_all_documents, delete_document
from services.onboarding_agent import answer_question, save_verified_qa, get_feedback_stats

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__,
    static_folder=os.path.join(BASE_DIR, "static"),
    template_folder=os.path.join(BASE_DIR, "templates")
)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

init_db()


# ── Admin auth decorator ──────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return decorated


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if password == ADMIN_PASSWORD:
            session["admin_authenticated"] = True
            next_url = request.args.get("next", url_for("onboarding_admin"))
            return redirect(next_url)
        flash("Incorrect password.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_authenticated", None)
    return redirect(url_for("onboarding"))


# ─── Ticket Routes ────────────────────────────────────────────────────────────

@app.route("/")
def index():
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


# ─── Ops Dashboard ────────────────────────────────────────────────────────────

@app.route("/ops")
def ops_dashboard():
    return render_template("ops_dashboard.html")


@app.route("/ops/query", methods=["POST"])
def ops_query():
    data     = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "Question is required."}), 400
    return jsonify(run_nl_query(question))


# ─── Onboarding ───────────────────────────────────────────────────────────────

@app.route("/onboarding")
def onboarding():
    return render_template("onboarding.html")


@app.route("/onboarding/chat", methods=["POST"])
def onboarding_chat():
    data     = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    history  = data.get("history", [])          # list of {question, answer}
    if not question:
        return jsonify({"error": "Question is required."}), 400
    try:
        result = answer_question(question, history=history)
        return jsonify(result)
    except Exception as e:
        return jsonify({
            "error":    str(e),
            "answer":   "Sorry, I encountered an error. Please try again.",
            "sources":  [],
            "has_docs": False,
        }), 500


@app.route("/onboarding/feedback", methods=["POST"])
def onboarding_feedback():
    data    = request.get_json(silent=True) or {}
    conv_id = data.get("conversation_id")
    if not conv_id:
        return jsonify({"error": "conversation_id required"}), 400

    db      = get_client_direct()
    payload = {"conversation_id": conv_id}

    rating = data.get("rating")
    if rating is not None:
        payload["rating"] = rating
        if rating == 1:
            # Thumbs up → save to verified Q&A knowledge base
            try:
                save_verified_qa(conv_id)
            except Exception:
                pass

    if data.get("is_flagged"):
        payload["is_flagged"]   = True
        payload["flag_comment"] = data.get("flag_comment", "")

    db.table("feedback").insert(payload).execute()
    return jsonify({"ok": True})


def get_client_direct():
    from models.db import get_client
    return get_client()


@app.route("/onboarding/upload", methods=["POST"])
@admin_required
def onboarding_upload():
    if "file" not in request.files:
        return jsonify({"error": "No file provided."}), 400

    f    = request.files["file"]
    name = f.filename or "upload"
    ext  = name.rsplit(".", 1)[-1].lower() if "." in name else "txt"

    if ext not in ("pdf", "docx", "txt", "md"):
        return jsonify({"error": "Unsupported file type. Use PDF, DOCX, TXT, or MD."}), 400

    file_bytes = f.read()
    if len(file_bytes) > 3 * 1024 * 1024:
        return jsonify({"error": "File too large. Maximum size is 3 MB."}), 400
    if len(file_bytes) == 0:
        return jsonify({"error": "File is empty."}), 400

    try:
        result = process_and_store(name, file_bytes, ext)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/onboarding/delete/<int:doc_id>", methods=["POST"])
@admin_required
def onboarding_delete(doc_id: int):
    try:
        delete_document(doc_id)
        flash("Document deleted successfully.", "success")
    except Exception as e:
        flash(f"Error deleting document: {str(e)}", "error")
    return redirect(url_for("onboarding_admin"))


@app.route("/onboarding/admin")
@admin_required
def onboarding_admin():
    documents = get_all_documents()
    stats     = get_feedback_stats()
    return render_template("onboarding_admin.html", documents=documents, stats=stats)


# ─── Run ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Starting AI Ticket Router...")
    app.run(debug=True, port=5000)
