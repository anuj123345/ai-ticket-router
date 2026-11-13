"""
Idempotent uploads and schema change detection.

Unit tests for services.schema_diff, and end-to-end tests of
process_and_store against a real local Postgres (the `pg` fixture).
"""
import pytest

from services import schema_diff


# -- schema_diff --------------------------------------------------------------

OLD = {"": [(0, "Region", "text"), (1, "Sales", "number"), (2, "Date", "date")]}


def test_same_layout_is_not_a_change():
    d = schema_diff.compare(OLD, {"": list(OLD[""])}, {"": 10}, {"": 12})
    assert d["changed"] is False
    assert schema_diff.summary(d) == ["The file: rows 10 → 12."]


def test_case_and_spacing_of_names_do_not_count():
    new = {"": [(0, " region ", "text"), (1, "SALES", "number"), (2, "Date", "date")]}
    assert schema_diff.compare(OLD, new)["changed"] is False


def test_added_removed_retyped_and_moved_columns():
    new = {"": [(0, "Sales", "text"), (1, "Region", "text"), (2, "Profit", "number")]}
    d = schema_diff.compare(OLD, new)
    s = d["sheets"][0]
    assert d["changed"]
    assert s["added"] == [{"name": "Profit", "type": "number"}]
    assert s["removed"] == [{"name": "Date", "type": "date"}]
    assert s["type_changed"] == [{"name": "Sales", "from": "number", "to": "text"}]
    assert s["reordered"] is True


def test_rename_detected_by_position_and_type():
    new = {"": [(0, "Region", "text"), (1, "Revenue", "number"), (2, "Date", "date")]}
    d = schema_diff.compare(OLD, new)
    s = d["sheets"][0]
    assert s["renamed"] == [{"from": "Sales", "to": "Revenue", "type": "number"}]
    assert s["added"] == [] and s["removed"] == []
    assert 'column "Sales" looks renamed to "Revenue".' in " ".join(schema_diff.summary(d))


def test_sheets_added_and_removed():
    d = schema_diff.compare({"Q1": OLD[""]}, {"Q2": OLD[""]})
    assert d["changed"]
    assert d["sheets_added"] == ["Q2"] and d["sheets_removed"] == ["Q1"]


# -- process_and_store, against real Postgres ---------------------------------

CSV_V1 = b"Region,Sales\nNorth,100\nSouth,250\n"
CSV_V2_MORE_ROWS = b"Region,Sales\nNorth,100\nSouth,250\nEast,75\n"
CSV_V3_NEW_COLUMN = b"Region,Sales,Profit\nNorth,100,10\nSouth,250,40\n"


@pytest.fixture
def store(pg, monkeypatch):
    from services import doc_processor, pipeline_log
    monkeypatch.setattr(doc_processor, "get_client", lambda: pg)
    monkeypatch.setattr(pipeline_log, "get_client", lambda: pg)
    return doc_processor


def _docs(pg):
    return pg.sql("select id, name from public.documents order by id")


def _runs(pg):
    return pg.sql("select pipeline, status, details->>'reason' from public.pipeline_runs order by id")


def test_same_file_twice_is_stored_once(store, pg):
    first = store.process_and_store("sales.csv", CSV_V1, "csv")
    again = store.process_and_store("sales copy.csv", CSV_V1, "csv")   # same bytes, any name

    assert again["duplicate"] is True
    assert again["document_id"] == first["document_id"]
    assert len(_docs(pg)) == 1
    assert pg.sql("select count(*) from public.datasets")[0][0] == 1
    assert _runs(pg) == [("upload", "success", None), ("upload", "skipped", "duplicate")]


def test_new_version_same_columns_replaces_old_after_storing_new(store, pg):
    first = store.process_and_store("sales.csv", CSV_V1, "csv")
    second = store.process_and_store("sales.csv", CSV_V2_MORE_ROWS, "csv")

    assert second["replaced"] == [first["document_id"]]
    assert second["changes"] == ["The file: rows 2 → 3."]
    assert [d[0] for d in _docs(pg)] == [second["document_id"]]
    # The old version's datasets and chunks went with it (cascade).
    assert pg.sql("select count(*) from public.datasets")[0][0] == 1
    assert pg.sql("select count(*) from public.doc_chunks where document_id = %s",
                  (first["document_id"],))[0][0] == 0


def test_changed_columns_need_confirmation_and_store_nothing(store, pg):
    first = store.process_and_store("sales.csv", CSV_V1, "csv")

    with pytest.raises(store.SchemaChangeNeedsConfirmation) as e:
        store.process_and_store("sales.csv", CSV_V3_NEW_COLUMN, "csv")
    assert 'The file: new column "Profit" (number).' in e.value.lines
    assert [d[0] for d in _docs(pg)] == [first["document_id"]]         # untouched
    assert _runs(pg)[-1] == ("upload", "skipped", "schema_change_unconfirmed")

    confirmed = store.process_and_store("sales.csv", CSV_V3_NEW_COLUMN, "csv", replace=True)
    assert confirmed["replaced"] == [first["document_id"]]
    assert [d[0] for d in _docs(pg)] == [confirmed["document_id"]]


def test_failed_upload_keeps_the_old_version_and_is_logged(store, pg, monkeypatch):
    first = store.process_and_store("sales.csv", CSV_V1, "csv")

    def broken(*a, **k):
        raise RuntimeError("database went away")
    monkeypatch.setattr(store, "store_chunks", broken)

    with pytest.raises(RuntimeError):
        store.process_and_store("sales.csv", CSV_V2_MORE_ROWS, "csv")
    assert [d[0] for d in _docs(pg)] == [first["document_id"]]
    assert _runs(pg)[-1][:2] == ("upload", "failed")


def test_works_before_the_migration_is_applied(store, pg):
    """Deployed code must not break uploads while content_hash doesn't exist yet."""
    pg.sql("reset role; drop index if exists public.documents_content_hash_key; "
           "alter table public.documents drop column content_hash; set role service_role")
    try:
        r = store.process_and_store("notes.txt", b"Some words long enough to be a chunk of text " * 5, "txt")
        assert r["document_id"]
    finally:
        pg.sql("reset role; alter table public.documents add column if not exists content_hash text; "
               "create unique index if not exists documents_content_hash_key on public.documents "
               "(content_hash) where content_hash is not null; set role service_role")


def test_concurrent_duplicate_is_caught_by_the_unique_index(store, pg, monkeypatch):
    store.process_and_store("sales.csv", CSV_V1, "csv")
    # Simulate the race: the hash lookup misses, then the insert hits the index.
    real = store.find_document_by_hash
    calls = {"n": 0}

    def miss_first(h):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(h)
    monkeypatch.setattr(store, "find_document_by_hash", miss_first)

    r = store.process_and_store("other-name.csv", CSV_V1, "csv")
    assert r["duplicate"] is True
    assert len(_docs(pg)) == 1
