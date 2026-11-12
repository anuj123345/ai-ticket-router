"""
Smart Search answers for spreadsheet questions (services/data_answer.py).

The model is replaced by a fixed plan (it only ever plans); everything after
that runs for real on both engines. The dataset is the 12-row workbook from
test_studio, with expected answers worked out by hand.
"""
import json
import types
from decimal import Decimal

import pytest

from services import ai_models, data_answer, dataset_store, studio, tabular
from tests.test_studio import workbook


@pytest.fixture(params=["ref", "pg"])
def engine(request):
    return request.getfixturevalue(request.param)


@pytest.fixture
def ds(engine, monkeypatch):
    sheets, _ = tabular.read_file(workbook(), "xlsx")
    a = tabular.analyse_sheet(sheets[0])
    doc_id = (engine.sql("insert into public.documents (name, file_type) values ('s.xlsx','xlsx') returning id")[0][0]
              if hasattr(engine, "sql") else 1)
    ds_id = dataset_store.save_dataset(doc_id, "s.xlsx", a, sheets[0].percent_cols)
    monkeypatch.setattr(data_answer, "datasets_for",
                        lambda names: [{"id": ds_id, "document_name": "s.xlsx", "label": "s.xlsx"}])
    return ds_id


@pytest.fixture
def planner(monkeypatch):
    """Make the model return a given plan, and record what it was shown."""
    seen = {}

    def set_plan(plan: dict):
        def fake(messages, **kw):
            seen["prompt"] = messages[-1]["content"]
            return json.dumps(plan)
        monkeypatch.setattr(ai_models, "chat_completion", fake)
    set_plan.seen = seen
    return set_plan


def ask(question="q"):
    return data_answer.answer(question, ["s.xlsx"])


# ── The audit's failures, reproduced and fixed ───────────────────────────────

def test_highest_by_group_in_a_month_range_is_exact(ds, planner):
    """Audit #1: 'highest Profit in August 2014' said Germany; it was the USA."""
    planner({"kind": "aggregate", "measure": {"agg": "sum", "column": "Sales"},
             "group_by": {"column": "Country"}, "order": "desc", "top": 1,
             "filters": [{"column": "Date", "op": "between", "from": "2024-02-01", "to": "2024-03-31"}]})
    out = ask()
    assert out["kind"] == "exact"
    # Feb-Mar: Nepal 45,000.50 (+ an N/A), Bhutan 10,000, India -5,000
    assert out["answer"].startswith("**Nepal** has the highest total Sales")
    assert "**45,000.50**" in out["answer"]
    assert "Next: Bhutan 10,000; India -5,000." in out["answer"]
    assert "4 of 12 rows matched" in out["answer"]


def test_same_question_always_gives_the_same_number(ds, planner):
    """Audit #2/#3: one question got 14, 200 and 'not found'."""
    planner({"kind": "aggregate", "measure": {"agg": "count_rows"},
             "filters": [{"column": "Segment", "op": "eq", "value": "government"}]})
    answers = {ask()["answer"] for _ in range(3)}
    assert len(answers) == 1
    assert answers.pop().startswith("**6** rows where Segment is \"Government\" (50.0% of all 12 rows).")


def test_counts_rows_not_text_chunks(ds, planner):
    """Audit #4: totals were chunk counts (115 instead of 700)."""
    planner({"kind": "aggregate", "measure": {"agg": "count_rows"}, "filters": []})
    assert ask()["answer"].startswith("There are **12** rows.")


def test_filters_match_real_values_not_keywords(ds, planner):
    """Audit #5: 'students' matched every row. Here "none" must mean the
    Discount Band value "None", not every row containing the word."""
    planner({"kind": "aggregate", "measure": {"agg": "sum", "column": "Sales"},
             "filters": [{"column": "discount band", "op": "eq", "value": "none"}]})
    out = ask()["answer"]
    # None rows: 120000, -5000, 2500.25, 3000
    assert "**120,500.25**" in out
    assert 'Discount Band is "None"' in out


def test_ambiguous_value_is_asked_about_not_guessed(ds, planner):
    planner({"kind": "aggregate", "measure": {"agg": "count_rows"},
             "filters": [{"column": "Country", "op": "eq", "value": "an"}]})
    out = ask()
    assert out["kind"] == "clarify"
    assert '"Bhutan"' in out["answer"] and '"France"' in out["answer"]


def test_value_that_does_not_exist_says_so(ds, planner):
    planner({"kind": "aggregate", "measure": {"agg": "count_rows"},
             "filters": [{"column": "Country", "op": "eq", "value": "Atlantis"}]})
    out = ask()
    assert out["kind"] == "clarify"
    assert out["answer"].startswith('No row has "Atlantis" in Country.')


def test_summing_a_text_column_is_refused_with_a_reason(ds, planner):
    planner({"kind": "aggregate", "measure": {"agg": "sum", "column": "Segment"}, "filters": []})
    assert "not a number column" in ask()["answer"]


def test_a_breakdown_over_months_reads_in_calendar_order(ds, planner):
    planner({"kind": "aggregate", "measure": {"agg": "count_rows"},
             "group_by": {"column": "Date", "grain": "month"}, "top": None, "filters": []})
    lines = [l for l in ask()["answer"].splitlines() if l.startswith("- ")]
    assert [l.split(":")[0] for l in lines] == ["- Jan 2024", "- Feb 2024", "- Mar 2024",
                                                 "- Apr 2024", "- May 2024", "- Jun 2024"]


def test_ordinal_number_filter(ds, planner):
    planner({"kind": "aggregate", "measure": {"agg": "count_rows"},
             "filters": [{"column": "Sale Price", "op": "gte", "value": "100th"}]})
    assert ask()["answer"].startswith("**3** rows where Sale Price ≥ 100")


def test_list_question_returns_the_rows(ds, planner):
    planner({"kind": "list", "filters": [{"column": "Country", "op": "eq", "value": "nepal"}]})
    out = ask()["answer"]
    assert out.startswith('**2** of 12 rows match: Country is "Nepal".')
    assert out.count("- Row ") == 2


def test_planner_sees_real_column_names_and_values(ds, planner):
    planner({"kind": "aggregate", "measure": {"agg": "count_rows"}, "filters": []})
    ask("how many rows?")
    prompt = planner.seen["prompt"]
    assert '"Discount Band" (text' in prompt and '"None"' in prompt
    assert '"Date" (date, 2024-01-05 to 2024-06-19)' in prompt


def test_question_not_about_the_table_falls_back(ds, planner):
    planner({"kind": "not_about_this_table"})
    assert ask() is None


# ── Wiring ───────────────────────────────────────────────────────────────────

def test_smart_search_uses_the_engine_and_never_lets_the_model_count(monkeypatch):
    from services import rag_graph
    stored = {}

    class FakeTable:
        def insert(self, row):
            stored.update(row)
            return self

        def execute(self):
            return types.SimpleNamespace(data=[{"id": 99}])

    monkeypatch.setattr(rag_graph, "get_client",
                        lambda: types.SimpleNamespace(table=lambda name: FakeTable()))
    monkeypatch.setattr(data_answer, "answer", lambda q, docs: {
        "answer": "**7** rows ...", "kind": "exact",
        "sources": [{"document_name": docs[0], "dataset_id": 5, "excerpt": "x"}]})
    called = []
    monkeypatch.setattr(ai_models, "chat_completion", lambda *a, **k: called.append(1) or "nope")
    out = rag_graph.run_rag("how many?", doc_filter="Sample data.xlsx")
    assert out["answer"] == "**7** rows ..."
    assert out["conversation_id"] == 99
    assert called == []                                   # the answer model was never asked
    assert json.loads(stored["sources"])[0]["dataset_id"] == 5


def test_thumbs_up_does_not_store_data_answers_as_verified(monkeypatch):
    from services import onboarding_agent
    writes = []

    class Q:
        def __init__(self, name):
            self.name = name

        def select(self, *a):
            return self

        def eq(self, *a):
            return self

        def insert(self, row):
            writes.append((self.name, row))
            return self

        def update(self, row):
            writes.append((self.name, row))
            return self

        def execute(self):
            if self.name == "conversations":
                return types.SimpleNamespace(data=[{
                    "question": "q", "answer": "**7** rows",
                    "sources": json.dumps([{"document_name": "s.xlsx", "dataset_id": 3}])}])
            return types.SimpleNamespace(data=[])

    monkeypatch.setattr(onboarding_agent, "get_client",
                        lambda: types.SimpleNamespace(table=lambda n: Q(n)))
    onboarding_agent.save_verified_qa(1)
    assert writes == []


def test_a_month_name_filter_repeating_the_date_range_is_dropped(ds, planner):
    """Live answer added Month Name = "August" on top of the August date range."""
    planner({"kind": "aggregate", "measure": {"agg": "count_rows"},
             "filters": [{"column": "Date", "op": "between", "from": "2024-02-01", "to": "2024-02-29"},
                         {"column": "Year", "op": "eq", "value": "2024"}]})
    out = ask()["answer"]
    assert "Year" not in out
    assert out.startswith("**2** rows where Date from 2024-02-01 to 2024-02-29")


@pytest.mark.parametrize("raw, expected", [
    ("</answer> Based on the data, 14 students.\nOUTDATED_FLAG: NO", "Based on the data, 14 students."),
    ("<answer> Found 7 of 200", "Found 7 of 200"),
    ("<answer>\nThe policy is X.\n</answer>\nOUTDATED_FLAG: NO", "The policy is X."),
    ("The 401(k) match is 4%. OUTDATED_FLAG: NO", "The 401(k) match is 4%."),     # conversation #4
    ("Leave is 16 weeks.\nOUTDATED_FLAG: YES\nOUTDATED_REASON: 2023 policy", "Leave is 16 weeks."),
])
def test_internal_tags_never_reach_the_user(raw, expected):
    from services.rag_graph import _parse_response
    assert _parse_response(raw)["answer"] == expected


def test_full_dates_given_for_a_year_column_mean_that_year(ds, planner):
    """Live check: 'most units in 2014' sent Year between 2014-01-01 and 2014-12-31."""
    planner({"kind": "aggregate", "measure": {"agg": "count_rows"},
             "filters": [{"column": "Year", "op": "between", "from": "2025-01-01", "to": "2025-12-31"}]})
    out = ask()["answer"]
    assert out.startswith("**2** rows where Year from 2025 to 2025")


def test_answer_links_to_exactly_its_rows(ds, planner):
    from urllib.parse import parse_qs, urlparse
    planner({"kind": "aggregate", "measure": {"agg": "count_rows"},
             "filters": [{"column": "Segment", "op": "eq", "value": "government"}]})
    src = ask()["sources"][0]
    url = urlparse(src["rows_url"])
    assert url.path == "/data-studio"
    q = parse_qs(url.query)
    filters = json.loads(q["filters"][0])
    assert q["dataset"] == [str(src["dataset_id"])] and q["rows"] == ["1"]
    assert src["rows_label"] == "See the 6 row(s) behind this answer"
    # the link's filters select the same 6 rows in Data Studio
    assert studio.drill_rows(src["dataset_id"], filters)["matched"] == 6
