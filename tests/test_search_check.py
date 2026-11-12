"""The live accuracy check must pass right answers and fail wrong ones."""
import io

import openpyxl
import pytest

from services import data_answer, dataset_store, search_check, tabular


def financial_sample() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Segment", "Country", "Product", "Discount Band", "Units Sold",
               "Sale Price", "Sales", "Profit", "Date"])
    rows = [
        ("Government", "Canada", "Paseo", "None", 100, 20, 2000, 500, "2014-08-03"),
        ("Government", "United States of America", "Paseo", "Low", 300, 15, 4500, 900, "2014-08-09"),
        ("Enterprise", "Canada", "VTT", "None", 50, 125, 6250, -200, "2013-12-01"),
        ("Midmarket", "Germany", "Amarilla", "High", 400, 30, 12000, 300, "2014-02-01"),
        ("Enterprise", "Mexico", "VTT", "Low", 220, 125, 27500, 1200, "2014-08-20"),
    ]
    for r in rows:
        ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def loaded(ref, monkeypatch):
    sheets, _ = tabular.read_file(financial_sample(), "xlsx")
    ds_id = dataset_store.save_dataset(1, search_check.DOCUMENT, tabular.analyse_sheet(sheets[0]), set())
    monkeypatch.setattr(data_answer, "datasets_for", lambda names: [
        {"id": ds_id, "document_name": search_check.DOCUMENT, "label": search_check.DOCUMENT}])
    return ds_id


def test_right_answers_pass(loaded, monkeypatch):
    from services import studio
    meta = studio.open_dataset(loaded)
    truths = {q: search_check._truth(loaded, meta, c) for q, c, _ in search_check.CASES}

    def perfect(question, docs):
        top, value, _ = truths[question]
        return {"answer": (f"**{top}** has it: **{value}**." if top else f"It is **{value}**.")}
    monkeypatch.setattr(data_answer, "answer", perfect)
    out = search_check.run()
    assert out["all_pass"], [r for r in out["results"] if not r["pass"]]
    by_q = {r["question"]: r["expected"] for r in out["results"]}
    assert by_q["Which country had the highest total Profit in August 2014?"] == "Mexico: 1,200"
    assert by_q["How many rows are in this data?"] == "5"
    assert by_q["Which segment had the lowest total Profit?"] == "Midmarket: 300"   # Enterprise is -200 + 1,200


def test_wrong_answers_fail(loaded, monkeypatch):
    monkeypatch.setattr(data_answer, "answer", lambda q, d: {"answer": "**Germany** has it: **26,164**."})
    out = search_check.run()
    assert out["passed"] == 0
