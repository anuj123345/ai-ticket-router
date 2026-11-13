"""
Shared fixtures.

`pg` gives a dataset_store wired to a real local Postgres with the Data Studio
migration applied, so the actual SQL engine is exercised. Tests that use it are
skipped when no database is reachable.

    TEST_DATABASE_URL="host=/tmp port=54329 dbname=solace_test user=postgres"
"""
import glob
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DSN = "host=/tmp port=54329 dbname=solace_test user=postgres"


@pytest.fixture(scope="session")
def pg_dsn():
    dsn = os.environ.get("TEST_DATABASE_URL", DEFAULT_DSN)
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=2)
    except Exception as e:
        pytest.skip(f"No test Postgres available ({type(e).__name__}); SQL engine tests skipped.")
    # The production tables that predate migrations, then the migrations the
    # tests rely on. All are safe to re-run.
    files = [ROOT / "supabase/baseline.sql"]
    files += sorted(pathlib.Path(p) for p in glob.glob(str(ROOT / "supabase/migrations/*data_studio*.sql")))
    files += sorted(pathlib.Path(p) for p in glob.glob(str(ROOT / "supabase/migrations/*data_platform*.sql")))
    with conn, conn.cursor() as cur:
        for path in files:
            cur.execute(path.read_text())
    conn.close()
    return dsn


@pytest.fixture
def pg(pg_dsn, monkeypatch):
    from services import dataset_store
    from tests.pg_client import PgClient
    client = PgClient(pg_dsn)
    client.sql("truncate public.datasets cascade")
    client.sql("truncate public.documents cascade")
    client.sql("reset role; truncate public.pipeline_runs; set role service_role")
    monkeypatch.setattr(dataset_store, "get_client", lambda: client)
    yield client
    client.conn.close()


@pytest.fixture
def ref(monkeypatch):
    """dataset_store wired to the in-memory reference engine (no database)."""
    from services import dataset_store
    from tests.ref_engine import RefClient
    client = RefClient()
    monkeypatch.setattr(dataset_store, "get_client", lambda: client)
    return client
