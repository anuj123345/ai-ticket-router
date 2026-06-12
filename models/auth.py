"""
Auth layer — user registration, login, session helpers.
Uses werkzeug password hashing (bundled with Flask).
"""
from werkzeug.security import generate_password_hash, check_password_hash
from models.db import get_client


def create_user(name: str, email: str, password: str) -> dict | None:
    """Create a new user. Returns user dict or None if email already exists."""
    client = get_client()
    # Check for duplicate email
    existing = client.table("users").select("id").eq("email", email.lower()).execute()
    if existing.data:
        return None  # email taken

    pw_hash = generate_password_hash(password)
    resp = client.table("users").insert({
        "name":          name.strip(),
        "email":         email.lower().strip(),
        "password_hash": pw_hash,
        "role":          "user",
    }).execute()
    return resp.data[0] if resp.data else None


def get_user_by_email(email: str) -> dict | None:
    client = get_client()
    resp = (
        client.table("users")
        .select("id, name, email, role, created_at")
        .eq("email", email.lower().strip())
        .execute()
    )
    return resp.data[0] if resp.data else None


def get_user_by_email_with_hash(email: str) -> dict | None:
    """Include password_hash — only used during login verification."""
    client = get_client()
    resp = (
        client.table("users")
        .select("*")
        .eq("email", email.lower().strip())
        .execute()
    )
    return resp.data[0] if resp.data else None


def get_user_by_id(user_id: int) -> dict | None:
    client = get_client()
    resp = (
        client.table("users")
        .select("id, name, email, role, created_at")
        .eq("id", user_id)
        .execute()
    )
    return resp.data[0] if resp.data else None


def verify_password(user: dict, password: str) -> bool:
    return check_password_hash(user.get("password_hash", ""), password)
