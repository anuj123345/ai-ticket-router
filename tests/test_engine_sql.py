"""
The SQL engine (ds_query in Postgres) against an independent answer key.

Every query is run twice: by Postgres, and by tests/ref_engine.py (plain
Python with Decimal, sharing no code with the SQL). Results must be identical.
A few are also checked against numbers worked out by hand.
"""
from decimal import Decimal

import pytest

from services import dataset_store, tabular
from tests import messy_files, ref_engine


def load(pg, file_bytes, file_type, name="f"):
    doc_id = pg.sql("insert into public.documents (name, file_type) values (%s, %s) returning id",
                    (name, file_type))[0][0]
    raw_sheets, _ = tabular.read_file(file_bytes, file_type)
    out = []
    for sh in raw_sheets:
        a = tabular.analyse_sheet(sh)
        ds_id = dataset_store.save_dataset(doc_id, name, a, sh.percent_cols)
        out.append((ds_id, a))
    return out


def as_ref(a):
    types = {c.pos: c.type for c in a.columns}
    data = [r["val"] for r in a.records if r["status"] == "data"]
    return types, data


def same_number(x, y):
    if x is None or y is None:
        return x is None and y is None
    try:
        return abs(Decimal(x) - Decimal(y)) <= Decimal("1e-12")
    except Exception:
        return x == y                       # dates come back as text


def assert_same(sql, ref, spec):
    assert sql["total"]["n"] == ref["total"]["n"], spec
    assert sql["group_count"] == ref["group_count"], spec
    assert len(sql["groups"]) == len(ref["groups"]), spec
    for s, r in zip([sql["total"]] + sql["groups"], [ref["total"]] + ref["groups"]):
        assert s.get("g") == r.get("g") and s.get("other") == r.get("other"), (spec, s, r)
        assert s["n"] == r["n"], (spec, s, r)
        assert len(s["m"]) == len(r["m"])
        for a, b in zip(s["m"], r["m"]):
            assert same_number(a, b), (spec, s, r)


# Columns of the big random file: 0 Order ID, 1 City, 2 Category, 3 Order Date,
# 4 Amount, 5 Qty, 6 Rate
ALL_MEASURES = [
    {"agg": "count_rows"}, {"agg": "sum", "col": 4}, {"agg": "avg", "col": 4},
    {"agg": "min", "col": 4}, {"agg": "max", "col": 4}, {"agg": "count", "col": 4},
    {"agg": "sum", "col": 5}, {"agg": "avg", "col": 6}, {"agg": "distinct", "col": 1},
    {"agg": "count", "col": 1}, {"agg": "min", "col": 3}, {"agg": "max", "col": 3},
    {"agg": "distinct", "col": 0},
]

SPECS = [
    {"measures": ALL_MEASURES},
    {"group_by": {"col": 1}, "measures": [{"agg": "sum", "col": 4}, {"agg": "count_rows"}]},
    {"group_by": {"col": 1}, "limit": 8, "measures": [{"agg": "sum", "col": 4}, {"agg": "avg", "col": 6}, {"agg": "distinct", "col": 0}]},
    {"group_by": {"col": 1}, "limit": 1, "measures": [{"agg": "count_rows"}]},
    {"group_by": {"col": 2}, "measures": [{"agg": "count_rows"}, {"agg": "avg", "col": 4}]},
    {"group_by": {"col": 2}, "limit": 2, "measures": [{"agg": "avg", "col": 6}, {"agg": "min", "col": 4}]},
    {"group_by": {"col": 3, "grain": "month"}, "measures": [{"agg": "sum", "col": 4}]},
    {"group_by": {"col": 3, "grain": "quarter"}, "measures": [{"agg": "sum", "col": 5}, {"agg": "max", "col": 3}]},
    {"group_by": {"col": 3, "grain": "year"}, "measures": [{"agg": "count_rows"}, {"agg": "distinct", "col": 1}]},
    {"group_by": {"col": 3, "grain": "day"}, "limit": 10, "measures": [{"agg": "sum", "col": 4}]},
    {"group_by": {"col": 5}, "measures": [{"agg": "sum", "col": 4}]},          # group by a number column
    {"filters": [{"col": 2, "op": "in", "values": ["A", None]}], "measures": ALL_MEASURES},
    {"filters": [{"col": 2, "op": "not_in", "values": ["A", None]}], "group_by": {"col": 2}, "measures": [{"agg": "count_rows"}]},
    {"filters": [{"col": 2, "op": "in", "values": [None]}], "measures": [{"agg": "count_rows"}, {"agg": "sum", "col": 4}]},
    {"filters": [{"col": 4, "op": "gte", "value": "1000"}, {"col": 5, "op": "lt", "value": "10"}], "group_by": {"col": 2}, "measures": [{"agg": "sum", "col": 4}]},
    {"filters": [{"col": 4, "op": "between", "from": "-100.5", "to": "250.125"}], "measures": [{"agg": "count_rows"}, {"agg": "sum", "col": 4}]},
    {"filters": [{"col": 3, "op": "between", "from": "2022-01-01", "to": "2022-12-31"}], "group_by": {"col": 3, "grain": "month"}, "measures": [{"agg": "sum", "col": 4}]},
    {"filters": [{"col": 3, "grain": "quarter", "op": "in", "values": ["2022-Q1", "2023-Q4"]}], "group_by": {"col": 1}, "limit": 5, "measures": [{"agg": "sum", "col": 4}]},
    {"filters": [{"col": 3, "grain": "year", "op": "in", "values": ["2023", None]}], "measures": [{"agg": "count_rows"}]},
    {"filters": [{"col": 1, "op": "contains", "value": "city 0"}], "group_by": {"col": 1}, "measures": [{"agg": "count_rows"}]},
    {"filters": [{"col": 4, "op": "blank"}], "measures": [{"agg": "count_rows"}, {"agg": "sum", "col": 4}]},
    {"filters": [{"col": 4, "op": "not_blank"}, {"col": 1, "op": "not_blank"}], "measures": [{"agg": "count_rows"}]},
    {"filters": [{"col": 1, "op": "in", "values": ["O'Brien", "Ünïcode Town"]}], "group_by": {"col": 1}, "measures": [{"agg": "sum", "col": 4}]},
    {"filters": [{"col": 4, "op": "eq", "value": "0"}, {"col": 4, "op": "ne", "value": "0"}], "group_by": {"col": 1}, "measures": [{"agg": "sum", "col": 4}]},   # matches nothing
    {"filters": [{"col": 1, "op": "in", "values": ["No Such City"]}], "measures": ALL_MEASURES},
]


@pytest.fixture
def big(pg):
    (ds_id, a), = load(pg, messy_files.big_random_csv(), "csv", "big.csv")
    return ds_id, a


def test_sql_engine_matches_the_answer_key_on_3000_messy_rows(big):
    ds_id, a = big
    types, data = as_ref(a)
    results = dataset_store.query(ds_id, SPECS)
    assert len(results) == len(SPECS)
    for spec, got in zip(SPECS, results):
        assert_same(got, ref_engine.run(types, data, spec), spec)


def test_sql_sum_equals_the_import_report_sum(big):
    ds_id, a = big
    amount = next(c for c in a.columns if c.name == "Amount")
    got, = dataset_store.query(ds_id, [{"measures": [{"agg": "sum", "col": 4}]}])
    assert Decimal(got["total"]["m"][0]) == Decimal(amount.stats["sum"])


def test_groups_plus_other_always_add_up_to_the_total(big):
    ds_id, _ = big
    got, = dataset_store.query(ds_id, [{"group_by": {"col": 1}, "limit": 6,
                                        "measures": [{"agg": "sum", "col": 4}, {"agg": "count_rows"}]}])
    assert len(got["groups"]) == 7 and got["groups"][-1]["other"] is True
    assert sum(Decimal(g["m"][0]) for g in got["groups"] if g["m"][0]) == Decimal(got["total"]["m"][0])
    assert sum(g["n"] for g in got["groups"]) == got["total"]["n"] == 3000


def test_hand_checked_sums_by_region(pg):
    (ds_id, a), _targets = load(pg, messy_files.sales_xlsx(), "xlsx", "sales.xlsx")
    got, = dataset_store.query(ds_id, [{"group_by": {"col": 0},
                                        "measures": [{"agg": "sum", "col": 5}, {"agg": "sum", "col": 4}]}])
    by = {g["g"]: g["m"] for g in got["groups"]}
    assert Decimal(by["East"][0]) == Decimal("3000.30")
    assert Decimal(by["West"][0]) == Decimal("3000.30")
    assert by["west"][0] is None                       # its only Amount was 'N/A'
    assert Decimal(by[None][0]) == Decimal("5000.50")  # blank region is its own group
    assert Decimal(by["North"][0]) == Decimal("6000.9")
    assert Decimal(got["total"]["m"][0]) == Decimal("17002")
    assert Decimal(got["total"]["m"][1]) == Decimal("360")    # Grand Total row not counted
    assert got["total"]["n"] == 8


def test_rows_behind_a_number_come_back_as_they_were_in_the_file(pg):
    (ds_id, _), _t = load(pg, messy_files.sales_xlsx(), "xlsx", "sales.xlsx")
    out = dataset_store.rows(ds_id, [{"col": 0, "op": "in", "values": ["North"]}])
    assert out["matched"] == 3
    assert [r["row"] for r in out["rows"]] == [10, 11, 12]     # Excel row numbers
    assert out["rows"][0]["cells"][5] == "6000.6"


def test_changing_settings_rebuilds_without_reupload(pg):
    sheets = load(pg, messy_files.indian_csv(), "csv", "indian.csv")
    ds_id = sheets[0][0]
    with pytest.raises(Exception, match="Cannot sum a text column"):
        dataset_store.query(ds_id, [{"measures": [{"agg": "sum", "col": 3}]}])

    report = dataset_store.rebuild_dataset(ds_id, {"columns": {"3": {"type": "number"}}})
    assert report["rows"]["loaded"] == 5
    got, = dataset_store.query(ds_id, [{"measures": [{"agg": "sum", "col": 3}, {"agg": "count", "col": 3}]}])
    assert Decimal(got["total"]["m"][0]) == Decimal("2674999.74")
    assert got["total"]["m"][1] == "4"                 # 'café' counts as blank

    dataset_store.rebuild_dataset(ds_id, {"include_totals": True})
    got, = dataset_store.query(ds_id, [{"measures": [{"agg": "count_rows"}, {"agg": "sum", "col": 2}]}])
    assert got["total"]["n"] == 6
    assert Decimal(got["total"]["m"][1]) == Decimal("9369135.50")   # doubled, as warned

    dataset_store.rebuild_dataset(ds_id, {"include_totals": False, "columns": {"3": {"type": "auto"}}})
    ds = dataset_store.get_dataset(ds_id)
    assert ds["row_count"] == 5 and ds["columns"][3]["type"] == "text"


def test_date_order_override_changes_the_months(pg):
    (ds_id, _), = load(pg, messy_files.ambiguous_dates_csv(), "csv", "amb.csv")
    q = [{"group_by": {"col": 1, "grain": "month"}, "measures": [{"agg": "sum", "col": 2}]}]
    assert sorted(g["g"] for g in dataset_store.query(ds_id, q)[0]["groups"]) == ["2024-04", "2024-06", "2024-08"]
    dataset_store.rebuild_dataset(ds_id, {"date_order": "MDY"})
    assert sorted(g["g"] for g in dataset_store.query(ds_id, q)[0]["groups"]) == ["2024-03", "2024-05", "2024-07"]


def test_filter_values_cannot_inject_sql(big, pg):
    ds_id, _ = big
    evil = "x'); drop table public.dataset_records; --"
    got, = dataset_store.query(ds_id, [{"filters": [
        {"col": 1, "op": "in", "values": [evil]},
        {"col": 1, "op": "contains", "value": evil}],
        "measures": [{"agg": "count_rows"}]}])
    assert got["total"]["n"] == 0
    assert pg.sql("select count(*) from public.dataset_records")[0][0] > 3000


@pytest.mark.parametrize("spec,message", [
    ({"measures": [{"agg": "sum", "col": 99}]}, "Unknown column"),
    ({"measures": [{"agg": "sum", "col": 1}]}, "Cannot sum a text column"),
    ({"measures": [{"agg": "median", "col": 4}]}, "Unknown aggregation"),
    ({"measures": []}, "at least one measure"),
    ({"filters": [{"col": 1, "op": "like", "value": "x"}], "measures": [{"agg": "count_rows"}]}, "Unknown filter operator"),
    ({"group_by": {"col": 3, "grain": "decade"}, "measures": [{"agg": "count_rows"}]}, "Unknown date grain"),
    ({"group_by": {"col": 1}, "limit": 0, "measures": [{"agg": "count_rows"}]}, "limit must be"),
])
def test_bad_requests_fail_loudly(big, spec, message):
    with pytest.raises(Exception, match=message):
        dataset_store.query(big[0], [spec])


def test_only_the_backend_role_can_use_the_engine(pg):
    for role, expected in (("anon", False), ("authenticated", False), ("service_role", True)):
        for fn in ("ds_query(bigint, jsonb)", "ds_rows(bigint, jsonb)",
                   "ds_put_records(bigint, jsonb)", "ds__run(bigint, jsonb, jsonb)"):
            assert pg.sql("select has_function_privilege(%s, %s, 'execute')",
                          (role, "public." + fn))[0][0] is expected, (role, fn)
        assert pg.sql("select has_table_privilege(%s, 'public.dataset_records', 'select')",
                      (role,))[0][0] is expected


def test_deleting_the_document_removes_its_datasets(pg):
    load(pg, messy_files.sales_xlsx(), "xlsx", "sales.xlsx")
    assert pg.sql("select count(*) from public.datasets")[0][0] == 2
    pg.sql("reset role; delete from public.documents; set role service_role")
    assert pg.sql("select count(*) from public.dataset_records")[0][0] == 0
