"""
Live accuracy check for Smart Search data answers (GET /admin/search-check).

Asks the real model a fixed set of questions about "Sample data.xlsx", and
checks each answer against the right figure computed directly by the engine
(no model involved). Nothing is stored. Run it after any model or prompt
change, and before showing the app to anyone.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from services import data_answer, dataset_store, studio

DOCUMENT = "Sample data.xlsx"

# question, then the engine query that gives the true answer (by column name),
# then what the answer must show: the top group, or the single total.
CASES = [
    ("How many rows are in this data?",
     {"measures": [("count_rows", None)]}, "total"),
    ("Which country had the highest total Profit in August 2014?",
     {"measures": [("sum", "Profit")], "group_by": "Country", "order": "desc",
      "filters": [("Date", "between", "2014-08-01", "2014-08-31")]}, "top"),
    ("What is the total Sales for the Government segment?",
     {"measures": [("sum", "Sales")], "filters": [("Segment", "in", ["Government"])]}, "total"),
    ("How many records have a Discount Band of None?",
     {"measures": [("count_rows", None)], "filters": [("Discount Band", "in", ["None"])]}, "total"),
    ("Which product sold the most units in 2014?",
     {"measures": [("sum", "Units Sold")], "group_by": "Product", "order": "desc",
      "filters": [("Date", "between", "2014-01-01", "2014-12-31")]}, "top"),
    ("What is the average Sale Price in Canada?",
     {"measures": [("avg", "Sale Price")], "filters": [("Country", "in", ["Canada"])]}, "total"),
    ("Which segment had the lowest total Profit?",
     {"measures": [("sum", "Profit")], "group_by": "Segment", "order": "asc"}, "top"),
    ("What were total Sales in December 2013?",
     {"measures": [("sum", "Sales")], "filters": [("Date", "between", "2013-12-01", "2013-12-31")]}, "total"),
]


def _truth(ds_id: int, meta: dict, case: dict) -> tuple[str | None, str, str]:
    """(top group or None, value as shown, aggregation) computed by the engine."""
    pos = {c["name"]: c for c in meta["columns"]}
    agg, col = case["measures"][0]
    measure = {"agg": agg} if col is None else {"agg": agg, "col": pos[col]["pos"]}
    filters = []
    for f in case.get("filters", []):
        name, op = f[0], f[1]
        if op == "between":
            filters.append({"col": pos[name]["pos"], "op": "between", "from": f[2], "to": f[3]})
        else:
            filters.append({"col": pos[name]["pos"], "op": op, "values": f[2]})
    spec = {"filters": filters, "measures": [measure]}
    if case.get("group_by"):
        spec["group_by"] = {"col": pos[case["group_by"]]["pos"]}
    res = dataset_store.query(ds_id, [spec])[0]
    fmt = pos[col]["format"] if col else "plain"
    shown_agg = "count" if agg in ("count_rows", "count", "distinct") else agg
    if case.get("group_by"):
        groups = [g for g in res["groups"] if g["g"] is not None and g["m"][0] is not None]
        from decimal import Decimal
        groups.sort(key=lambda g: Decimal(g["m"][0]), reverse=case.get("order") != "asc")
        top = groups[0]
        return top["g"], studio.fmt_number(top["m"][0], fmt, shown_agg), agg
    return None, studio.fmt_number(res["total"]["m"][0] if col else res["total"]["n"], fmt, shown_agg), agg


def run() -> dict:
    sets = data_answer.datasets_for([DOCUMENT])
    if not sets:
        return {"error": f'Upload "{DOCUMENT}" (or open it once in Data Studio) to run this check.'}
    ds = sets[0]
    meta = studio.open_dataset(ds["id"])

    def one(item):
        question, case, expect = item
        top, value, _ = _truth(ds["id"], meta, case)
        try:
            out = data_answer.answer(question, [DOCUMENT])
            text = (out or {}).get("answer") or "(no answer: fell back to document search)"
        except Exception as e:
            text = f"(error: {type(e).__name__}: {e})"
        ok = f"**{value}**" in text and (top is None or f"**{top}**" in text)
        return {"question": question, "pass": ok,
                "expected": (f"{top}: {value}" if top else value), "answer": text}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, CASES))
    passed = sum(r["pass"] for r in results)
    return {"dataset": ds["label"], "passed": passed, "total": len(results),
            "all_pass": passed == len(results), "results": results}
