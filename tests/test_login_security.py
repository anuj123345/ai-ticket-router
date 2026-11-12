"""Login redirects stay on this site; admin password has no default."""
import pytest


@pytest.fixture
def app_mod(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "sb_secret_dummy")
    import app as a
    return a


@pytest.mark.parametrize("target", [
    "https://evil.example/login", "//evil.example", "/\\evil.example", "\\\\evil.example",
    "http:/evil.example", "javascript:alert(1)", "evil.example", " //evil.example",
    "/ok\r\nSet-Cookie: x=1", "", None,
])
def test_outside_or_odd_targets_fall_back_home(app_mod, target):
    assert app_mod.safe_next(target, "/welcome") == "/welcome"


@pytest.mark.parametrize("target", ["/data-studio", "/onboarding/admin", "/admin/health?x=1"])
def test_paths_on_this_site_are_kept(app_mod, target):
    assert app_mod.safe_next(target, "/welcome") == target


def test_admin_login_ignores_a_foreign_next(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "ADMIN_PASSWORD", "s3cret-pass")
    c = app_mod.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
    r = c.post("/admin/login?next=https://evil.example", data={"password": "s3cret-pass"})
    assert r.status_code == 302
    assert r.headers["Location"].endswith("/onboarding/admin")


def test_admin_login_is_closed_when_no_password_is_configured(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "ADMIN_PASSWORD", "")
    c = app_mod.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
    for guess in ("", "admin123"):
        c.post("/admin/login", data={"password": guess})
        with c.session_transaction() as s:
            assert not s.get("admin_authenticated")


def test_user_login_redirect_and_fresh_session(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "get_user_by_email_with_hash",
                        lambda e: {"id": 7, "name": "A", "email": e, "role": "user"})
    monkeypatch.setattr(app_mod, "verify_password", lambda u, p: True)
    c = app_mod.app.test_client()
    with c.session_transaction() as s:
        s["planted"] = "by-attacker"
    r = c.post("/login?next=//evil.example", data={"email": "a@b.c", "password": "x"})
    assert r.headers["Location"].endswith("/welcome")
    with c.session_transaction() as s:
        assert s.get("user_id") == 7 and "planted" not in s


def test_login_form_keeps_a_safe_destination_and_drops_a_foreign_one(app_mod):
    c = app_mod.app.test_client()
    html = c.get("/login?next=/data-studio").get_data(as_text=True)
    assert 'action="/login?next=/data-studio"' in html
    html = c.get("/login?next=https://evil.example").get_data(as_text=True)
    assert 'action="/login"' in html and "evil" not in html
