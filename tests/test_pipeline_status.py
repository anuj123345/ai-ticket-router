"""Admin Pipeline page: checks built from pipeline_status(), and the page itself."""
from datetime import datetime, timedelta, timezone

import pytest

from services import pipeline_status

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


def healthy():
    return {
        "bronze_tickets": 7, "gold_tickets": 7,
        "bronze_max_updated_at": iso(NOW - timedelta(hours=5)),
        "gold_max_updated_at":   iso(NOW - timedelta(hours=5)),
        "last_dbt": {"status": "success", "started_at": iso(NOW - timedelta(minutes=21)),
                     "finished_at": iso(NOW - timedelta(minutes=20)),
                     "details": {"models": 10, "tests": 56, "failed": 0, "warned": 1}},
        "by_department": [{"department": "IT", "tickets": 6, "resolved": 1, "avg_resolution_hours": 0.01}],
        "runs": [],
    }


def check(result, name):
    return next(c for c in result["checks"] if c["name"] == name)


def test_healthy_pipeline_passes_every_check():
    r = pipeline_status.build(healthy(), NOW)
    assert r["all_ok"], r["checks"]
    assert check(r, "Reconciliation")["text"] == "Gold has all 7 tickets that bronze has."
    assert check(r, "Last dbt build")["text"].startswith("Success 20 min ago: 10 models, 56 tests")


def test_count_mismatch_fails_reconciliation():
    raw = healthy() | {"gold_tickets": 6}
    r = pipeline_status.build(raw, NOW)
    assert not check(r, "Reconciliation")["ok"]
    assert not r["all_ok"]


def test_change_not_yet_in_gold_shows_lag():
    raw = healthy() | {"bronze_max_updated_at": iso(NOW - timedelta(minutes=10))}
    assert not check(pipeline_status.build(raw, NOW), "Lag")["ok"]


def test_no_run_for_hours_is_stale():
    raw = healthy()
    raw["last_dbt"] = raw["last_dbt"] | {"finished_at": iso(NOW - timedelta(hours=4))}
    assert not check(pipeline_status.build(raw, NOW), "Freshness")["ok"]


def test_failed_build_shows_its_error():
    raw = healthy()
    raw["last_dbt"] = raw["last_dbt"] | {"status": "failed", "error": "unique_fct_tickets_ticket_id: Got 1 result"}
    c = check(pipeline_status.build(raw, NOW), "Last dbt build")
    assert not c["ok"] and "unique_fct_tickets_ticket_id" in c["error"]


def test_before_first_dbt_run():
    r = pipeline_status.build({"bronze_tickets": 7, "runs": []}, NOW)
    assert not check(r, "Gold layer")["ok"]
    assert not check(r, "Last dbt build")["ok"]


def test_upload_runs_are_described():
    raw = healthy()
    raw["runs"] = [
        {"pipeline": "upload", "run_key": "sales.csv", "status": "skipped",
         "started_at": iso(NOW - timedelta(minutes=2)), "finished_at": iso(NOW - timedelta(minutes=2)),
         "details": {"reason": "duplicate"}},
        {"pipeline": "dbt", "run_key": "abc", "status": "success", "rows_out": 512,
         "started_at": iso(NOW - timedelta(minutes=21)), "finished_at": iso(NOW - timedelta(minutes=20)),
         "details": {"command": "build", "target": "prod", "models": 10, "tests": 56, "warned": 1}},
    ]
    runs = pipeline_status.build(raw, NOW)["runs"]
    assert runs[0]["summary"] == "Same file already uploaded; nothing changed"
    assert runs[1]["summary"] == "build: 10 models, 56 tests, 1 warning"
    assert runs[1]["seconds"] == 60.0
    assert runs[1]["name"] == "scheduled build"


def test_sql_function_runs_before_gold_exists(pg):
    pg.sql("reset role; drop schema if exists gold cascade; create schema gold; "
           "alter schema gold owner to dbt_runner; set role service_role")
    raw = pg.rpc("pipeline_status", {}).execute().data
    assert "gold_tickets" not in raw
    assert raw["runs"] == []
    r = pipeline_status.build(raw, NOW)
    assert not check(r, "Gold layer")["ok"]


def test_function_is_not_callable_by_anon(pg):
    pg.sql("reset role")
    allowed = pg.sql("select has_function_privilege(r, 'public.pipeline_status()', 'execute') "
                     "from unnest(array['anon', 'authenticated', 'service_role']) r")
    pg.sql("set role service_role")
    assert [a[0] for a in allowed] == [False, False, True]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "sb_secret_dummy")
    import app as a
    c = a.app.test_client()
    return a, c


def test_page_needs_admin(client):
    _, c = client
    with c.session_transaction() as s:
        s["user_id"] = 1
    assert c.get("/admin/pipeline").status_code == 302


def test_page_renders_checks_and_runs(client, monkeypatch):
    a, c = client
    monkeypatch.setattr(pipeline_status, "load", lambda: pipeline_status.build(healthy(), NOW))
    with c.session_transaction() as s:
        s["user_id"], s["admin_authenticated"] = 1, True
    html = c.get("/admin/pipeline").get_data(as_text=True)
    assert "Gold has all 7 tickets that bronze has." in html
    assert "All passing" in html


def test_page_explains_missing_migration(client, monkeypatch):
    a, c = client

    def missing():
        raise RuntimeError("Could not find the function public.pipeline_status without parameters")
    monkeypatch.setattr(pipeline_status, "load", missing)
    with c.session_transaction() as s:
        s["user_id"], s["admin_authenticated"] = 1, True
    html = c.get("/admin/pipeline").get_data(as_text=True)
    assert "20261114100000_data_platform.sql" in html
