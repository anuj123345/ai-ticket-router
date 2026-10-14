"""
Tests for resumable embedding (services/doc_processor.py).

Uses an in-memory fake of the Supabase client and the NVIDIA embedding API,
so they run offline:  python -m pytest tests/test_embedding.py -q
"""
import types

import pytest

from services import ai_models
from services import doc_processor as dp


# ── Fakes ────────────────────────────────────────────────────────────────────

class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.filters, self._limit, self._count = [], None, False

    def select(self, _cols, count=None, head=None):
        self._count = count == "exact"
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r[col] == val)
        return self

    def is_(self, col, val):
        assert val == "null"
        self.filters.append(lambda r: r.get(col) is None)
        return self

    def order(self, col):
        self._order = col
        return self

    def limit(self, n):
        self._limit = n
        return self

    def insert(self, rows):
        self._insert = rows
        return self

    def execute(self):
        if hasattr(self, "_insert"):
            for r in self._insert:
                self.db.next_id += 1
                self.db.chunks.append({"id": self.db.next_id, "embedding": None, **r})
            return types.SimpleNamespace(data=self._insert, count=None)
        rows = [r for r in self.db.chunks if all(f(r) for f in self.filters)]
        rows.sort(key=lambda r: r["chunk_index"])
        if self._limit is not None:
            rows = rows[: self._limit]
        return types.SimpleNamespace(data=rows, count=len(rows) if self._count else None)


class FakeDB:
    def __init__(self, n_chunks=0, doc_id=1):
        self.chunks, self.next_id = [], 0
        for i in range(n_chunks):
            self.next_id += 1
            self.chunks.append({"id": self.next_id, "document_id": doc_id,
                                "chunk_index": i, "content": f"chunk {i}", "embedding": None})
        self.rpc_calls = 0

    def table(self, name):
        assert name == "doc_chunks"
        return FakeQuery(self, name)

    def rpc(self, name, params):
        assert name == "set_chunk_embeddings"
        assert params["model"]          # model name is recorded with each embedding
        self.rpc_calls += 1
        n = 0
        for item in params["items"]:
            for r in self.chunks:
                if r["id"] == item["id"] and r["embedding"] is None:
                    r["embedding"] = item["embedding"]
                    n += 1
        return types.SimpleNamespace(execute=lambda: types.SimpleNamespace(data=n))


class HTTPError(Exception):
    """Shaped like openai.APIStatusError: has status_code and body."""
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status_code, self.body = status, {"detail": detail}


class FakeNIM:
    """fail_contents: chunk texts whose batch always fails (503).
    down: everything fails (503). retired: everything fails with 410 Gone."""
    def __init__(self, fail_contents=(), down=False, retired=False, dims=2048):
        self.fail_contents, self.down, self.retired = set(fail_contents), down, retired
        self.dims, self.calls = dims, 0
        self.embeddings = self

    def create(self, model, input, **_):
        self.calls += 1
        if self.retired:
            raise HTTPError(410, f"The model '{model}' has reached its end of life")
        if self.down or self.fail_contents.intersection(input):
            raise HTTPError(503, "Service Unavailable")
        data = [types.SimpleNamespace(index=i, embedding=[0.5] * self.dims) for i in range(len(input))]
        return types.SimpleNamespace(data=list(reversed(data)))   # out of order on purpose


@pytest.fixture
def wire(monkeypatch):
    def _wire(db, nim):
        monkeypatch.setattr(dp, "get_client", lambda: db)
        monkeypatch.setattr(dp, "_embed_client", lambda: nim)
        monkeypatch.setattr(dp.time, "sleep", lambda s: None)
    return _wire


# ── Tests ────────────────────────────────────────────────────────────────────

def test_store_chunks_inserts_text_only_in_batches(wire):
    db = FakeDB()
    wire(db, FakeNIM())
    dp.store_chunks(7, "doc.md", [f"c{i}" for i in range(450)])
    assert len(db.chunks) == 450
    assert all(c["embedding"] is None for c in db.chunks)
    assert [c["chunk_index"] for c in db.chunks] == list(range(450))


def test_embeds_everything_when_service_is_healthy(wire):
    db = FakeDB(n_chunks=120)
    wire(db, FakeNIM())
    out = dp.embed_pending_chunks(1)
    assert out == {"embedded": 120, "failed": 0, "remaining": 0}
    assert all(c["embedding"] is not None for c in db.chunks)


def test_bad_batch_is_skipped_and_the_rest_still_gets_embedded(wire):
    db = FakeDB(n_chunks=120)
    wire(db, FakeNIM(fail_contents={"chunk 0"}))   # first batch (0-49) always fails
    out = dp.embed_pending_chunks(1)
    assert out["embedded"] == 70
    assert out["failed"] == 50
    assert out["remaining"] == 50          # left pending for a later retry, not lost


def test_service_outage_reports_no_progress(wire):
    db = FakeDB(n_chunks=60)
    nim = FakeNIM(down=True)
    wire(db, nim)
    out = dp.embed_pending_chunks(1)
    assert out["embedded"] == 0
    assert out["remaining"] == 60
    assert nim.calls == 2 * dp.EMBED_ATTEMPTS   # 2 batches, each retried once


def test_resuming_picks_up_where_it_stopped(wire):
    db = FakeDB(n_chunks=100)
    nim = FakeNIM(down=True)
    wire(db, nim)
    assert dp.embed_pending_chunks(1)["remaining"] == 100
    nim.down = False
    assert dp.embed_pending_chunks(1) == {"embedded": 100, "failed": 0, "remaining": 0}


def test_never_starts_a_batch_that_could_overrun_the_limit(wire, monkeypatch):
    db = FakeDB(n_chunks=500)
    wire(db, FakeNIM())
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 5.0            # every check costs 5 "seconds"
        return clock["t"]

    monkeypatch.setattr(dp.time, "monotonic", fake_monotonic)
    out = dp.embed_pending_chunks(1, limit_s=dp.REQUEST_LIMIT_S)
    # Budget: start + worst batch must stay under the limit.
    assert 0 < out["embedded"] < 500
    assert out["remaining"] == 500 - out["embedded"]


def test_worst_case_batch_fits_inside_vercel_limit():
    assert dp.REQUEST_LIMIT_S < 60
    assert dp.WORST_BATCH_S < dp.REQUEST_LIMIT_S


def test_retired_model_stops_immediately_with_the_reason(wire):
    db = FakeDB(n_chunks=120)
    nim = FakeNIM(retired=True)
    wire(db, nim)
    out = dp.embed_pending_chunks(1)
    assert nim.calls == 1                      # no retry, no further batches
    assert out["embedded"] == 0 and out["remaining"] == 120
    assert "end of life" in out["error"]


def test_embeddings_are_trimmed_to_column_size_and_normalised(wire):
    db = FakeDB(n_chunks=3)
    wire(db, FakeNIM(dims=2048))
    dp.embed_pending_chunks(1)
    vec = db.chunks[0]["embedding"]
    assert len(vec) == ai_models.EMBED_DIM
    assert abs(sum(x * x for x in vec) - 1.0) < 1e-9


def test_model_returning_too_few_dimensions_is_a_clear_error(wire):
    db = FakeDB(n_chunks=3)
    wire(db, FakeNIM(dims=768))
    out = dp.embed_pending_chunks(1)
    assert out["embedded"] == 0
    assert "768 dimensions" in out["error"]


def test_chat_picker_maps_old_saved_model_ids_to_a_working_tier():
    from services import rag_graph
    assert rag_graph.resolve_model_tier("power") == "power"
    assert rag_graph.resolve_model_tier("meta/llama-3.1-8b-instruct") == rag_graph.DEFAULT_MODEL
    assert rag_graph.resolve_model_tier(None) == rag_graph.DEFAULT_MODEL
    assert rag_graph.resolve_model_tier(rag_graph.POWER_MODEL) == "power"
