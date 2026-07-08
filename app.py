"""
AI Ticket Router — Flask Application
"""

import json
import os
from datetime import datetime
from functools import wraps
from flask import (Flask, render_template, request, redirect,
                   url_for, flash, jsonify, session)
from dotenv import load_dotenv

from models.db import init_db, create_ticket, get_ticket, get_all_tickets, update_ticket_status, get_stats
from models.auth import create_user, get_user_by_email_with_hash, get_user_by_id, verify_password
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


# ── Auth decorators ───────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", next=request.path))
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin_login", next=request.path))
        return f(*args, **kwargs)
    return decorated


# ── User auth routes ──────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("welcome"))

    error = None
    email = ""
    if request.method == "POST":
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        user = get_user_by_email_with_hash(email)
        if user and verify_password(user, password):
            session["user_id"]   = user["id"]
            session["user_name"] = user["name"]
            session["user_email"]= user["email"]
            session["user_role"] = user.get("role", "user")
            next_url = request.args.get("next", url_for("welcome"))
            return redirect(next_url)
        else:
            error = "Invalid email or password."

    return render_template("login.html", error=error, email=email)


@app.route("/register", methods=["GET", "POST"])
def register():
    if session.get("user_id"):
        return redirect(url_for("welcome"))

    error = None
    name = email = ""
    if request.method == "POST":
        name     = request.form.get("name", "").strip()
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not all([name, email, password]):
            error = "All fields are required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        else:
            user = create_user(name, email, password)
            if user is None:
                error = "An account with this email already exists."
            else:
                session["user_id"]    = user["id"]
                session["user_name"]  = user["name"]
                session["user_email"] = user["email"]
                session["user_role"]  = user.get("role", "user")
                return redirect(url_for("welcome"))

    return render_template("register.html", error=error, name=name, email=email)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Welcome dashboard ─────────────────────────────────────────────────────────

@app.route("/welcome")
@login_required
def welcome():
    stats     = get_stats()
    fb_stats  = get_feedback_stats()
    doc_count = len(get_all_documents())

    hour = datetime.now().hour
    if hour < 12:
        time_of_day = "morning"
    elif hour < 17:
        time_of_day = "afternoon"
    else:
        time_of_day = "evening"

    user = {
        "name":  session.get("user_name", "there"),
        "email": session.get("user_email", ""),
        "role":  session.get("user_role", "user"),
    }

    return render_template("welcome.html",
        user=user,
        stats=stats,
        doc_count=doc_count,
        qa_count=fb_stats.get("qa_count", 0),
        time_of_day=time_of_day,
    )


# ── Admin login (for doc management) ─────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
@login_required
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


# ─── Public landing ──────────────────────────────────────────────────────────

@app.route("/")
def landing():
    if session.get("user_id"):
        return redirect(url_for("welcome"))
    return render_template("landing.html")


@app.route("/use-cases")
def use_cases():
    return render_template("use_cases.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")

@app.route("/cookies")
def cookies_policy():
    return render_template("cookies.html")


@app.route("/api/public-stats")
def public_stats():
    """Public stats endpoint — no login required. Safe to expose on landing page."""
    try:
        s = get_stats()
        return jsonify({
            "total":    s.get("total", 0),
            "resolved": s.get("by_status", {}).get("resolved", 0),
            "pending":  s.get("by_status", {}).get("pending_review", 0),
            "depts":    len([k for k, v in s.get("by_dept", {}).items() if v > 0]),
        })
    except Exception:
        return jsonify({"total": 0, "resolved": 0, "pending": 0, "depts": 0})


# ─── Ticket Routes ────────────────────────────────────────────────────────────

@app.route("/submit-ticket")
@login_required
def index():
    return render_template("index.html")


@app.route("/submit", methods=["POST"])
@login_required
def submit_ticket():
    subject     = request.form.get("subject",     "").strip()
    description = request.form.get("description", "").strip()
    submitter   = request.form.get("submitter",   "").strip()
    email       = request.form.get("email",       "").strip()

    if not all([subject, description, submitter, email]):
        flash("All fields are required.", "error")
        return redirect(url_for("index"))  # /submit-ticket

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
        return redirect(url_for("index"))  # /submit-ticket


@app.route("/review/<int:ticket_id>")
@login_required
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
@login_required
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
@login_required
def reject_ticket(ticket_id: int):
    update_ticket_status(ticket_id, status="escalated")
    flash(f"Ticket #{ticket_id} escalated for manual review.", "warning")
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
@login_required
def dashboard():
    tickets = get_all_tickets(limit=100)
    stats   = get_stats()
    return render_template("dashboard.html", tickets=tickets, stats=stats)


@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify(get_stats())


@app.route("/ticket/<int:ticket_id>")
@login_required
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
@login_required
def ops_dashboard():
    return render_template("ops_dashboard.html")


@app.route("/ops/query", methods=["POST"])
@login_required
def ops_query():
    data     = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "Question is required."}), 400
    return jsonify(run_nl_query(question))


# ─── Onboarding ───────────────────────────────────────────────────────────────

@app.route("/onboarding")
@login_required
def onboarding():
    return render_template("onboarding.html")


@app.route("/onboarding/chat", methods=["POST"])
@login_required
def onboarding_chat():
    data     = request.get_json(silent=True) or {}
    question = data.get("question", "").strip()
    history  = data.get("history", [])
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
@login_required
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

    if ext not in ("pdf", "docx", "txt", "md", "xlsx"):
        return jsonify({"error": "Unsupported file type. Use PDF, DOCX, TXT, MD, or XLSX."}), 400

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
