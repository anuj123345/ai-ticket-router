"""
Data Studio dashboards (services/studio.py), end to end:
file bytes -> tabular import -> stored dataset -> engine -> dashboard JSON.

Every test runs twice: on the real Postgres engine (`pg`, skipped if no local
database) and on the independent Python reference engine (`ref`). Expected
numbers are worked out by hand from the rows written below.
"""
import io
from decimal import Decimal

import openpyxl
import pytest

from services import dataset_store, studio, tabular

# 12 data rows under a title, with a Grand Total row that must not be counted.
# Sales has a rupee value, an Indian-grouped value, a bracket negative and an
# "N/A"; Discount Band has a real category called "None"; Country has one blank.
ROWS = [
    # Country,  Segment,     Discount Band, Sales,         Sale Price, Date,         Year
    ("India",   "Government", "None",   "₹1,20,000",       "10",   "05/01/2024", "2024"),
    ("India",   "Enterprise", "Low",    "30000",           "20",   "17/01/2024", "2024"),
    ("India",   "Government", "None",   "(5,000)",         "30",   "02/02/2024", "2024"),
    ("Nepal",   "Government", "High",   "45000.50",        "40",   "14/02/2024", "2024"),
    ("Nepal",   "Enterprise", "Low",    "N/A",             "50",   "03/03/2024", "2024"),
    ("Bhutan",  "Enterprise", "Low",    "10000",           "60",   "21/03/2024", "2024"),
    ("Bhutan",  "Government", "None",   "2,500.25",        "70",   "09/04/2024", "2024"),
    ("Spain",   "Enterprise", "High",   "1000",            "80",   "13/04/2024", "2024"),
    ("France",  "Government", "Low",    "2000",            "90",   "15/05/2024", "2024"),
    ("Italy",   "Enterprise", "None",   "3000",            "100",  "16/05/2024", "2024"),
    ("Chile",   "Government", "Low",    "4000",            "110",  "18/06/2024", "2025"),
    (None,      "Enterprise", "High",   "500",             "120",  "19/06/2024", "2025"),
]
SALES_TOTAL = Decimal("120000") + 30000 - 5000 + Decimal("45000.50") + 10000 \
    + Decimal("2500.25") + 1000 + 2000 + 3000 + 4000 + 500          # N/A is blank
SALE_PRICE_AVG = Decimal(sum(range(10, 121, 10))) / 12


def workbook() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Regional sales report FY 2024"])
    ws.append([])
    ws.append(["Country", "Segment", "Discount Band", "Sales", "Sale Price", "Date", "Year"])
    for r in ROWS:
        ws.append(list(r))
    ws.append(["Grand Total", None, None, "999999", None, None, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture(params=["ref", "pg"])
def engine(request):
    return request.getfixturevalue(request.param)


@pytest.fixture
def ds(engine):
    sheets, _ = tabular.read_file(workbook(), "xlsx")
    a = tabular.analyse_sheet(sheets[0])
    if hasattr(engine, "sql"):           # real Postgres needs a documents row
        doc_id = engine.sql("insert into public.documents (name, file_type) values ('s.xlsx','xlsx') returning id")[0][0]
    else:
        doc_id = 1
    return dataset_store.save_dataset(doc_id, "s.xlsx", a, sheets[0].percent_cols)


def pos(meta, name):
    return next(c["pos"] for c in meta["columns"] if c["name"] == name)


def chart(dash, title_start):
    return next(c for c in dash["charts"] if c["title"].startswith(title_start))


# ── Import feeds the dashboard correctly ─────────────────────────────────────

def test_column_roles_are_sensible(ds):
    meta = studio.open_dataset(ds)
    by = {c["name"]: c for c in meta["columns"]}
    assert meta["rows"] == 12                                  # title, header, total excluded
    assert by["Sales"]["type"] == "number" and by["Sales"]["default_agg"] == "sum"
    assert by["Sale Price"]["default_agg"] == "avg"
    assert by["Year"]["default_agg"] == "none"                 # never summed
    assert by["Date"]["type"] == "date"
    assert by["Discount Band"]["stats"]["top"][0] == {"value": "Low", "count": 5}
    assert {"value": "None", "count": 4} in by["Discount Band"]["stats"]["top"]


def test_kpis_are_exact(ds):
    dash = studio.build(ds)
    k = {x["label"]: x for x in dash["kpis"]}
    assert k["Rows"]["exact"] == "12"
    assert Decimal(k["Total Sales"]["exact"]) == SALES_TOTAL     # 213000.75
    assert k["Total Sales"]["value"] == "213,000.75"          # ₹ on only 1 of 11 values: plain
    assert k["Total Sales"]["sub"] == "1 row(s) blank, not counted"
    assert Decimal(k["Average Sale Price"]["exact"]) == SALE_PRICE_AVG
    assert not any("Year" in label for label in k)              # a year is not a KPI


def test_every_auto_chart_adds_up(ds):
    dash = studio.build(ds)
    assert dash["charts"], "auto dashboard should not be empty"
    for c in dash["charts"]:
        if c["check"] and c["spec"]["agg"] != "avg":
            assert c["check"]["ok"], (c["title"], c["check"])


def test_dates_trend_by_month_in_order_with_day_first_dates(ds):
    dash = studio.build(ds)
    line = next(c for c in dash["charts"] if c["type"] == "line")
    assert line["labels"] == ["Jan 2024", "Feb 2024", "Mar 2024", "Apr 2024", "May 2024", "Jun 2024"]
    jan = Decimal(120000 + 30000)
    assert Decimal(str(line["datasets"][0]["data"][0])) == jan


def test_top_n_plus_other_and_blank_group(ds):
    meta = studio.open_dataset(ds)
    dash = studio.build(ds, charts=[{"x": pos(meta, "Country"), "agg": "count_rows", "type": "bar", "top": 3}])
    c = dash["charts"][0]
    assert c["labels"][:3] == ["India", "Bhutan", "Nepal"]
    assert c["labels"][3].startswith("Other (")
    assert sum(c["rows"]) == 12
    assert c["check"]["ok"]
    # the blank Country row is inside Other here; ask for all groups to see it
    dash = studio.build(ds, charts=[{"x": pos(meta, "Country"), "agg": "count_rows", "type": "bar", "top": 50}])
    assert "(Blank)" in dash["charts"][0]["labels"]


def test_drill_through_returns_exactly_the_rows_behind_a_bar(ds):
    meta = studio.open_dataset(ds)
    dash = studio.build(ds, charts=[{"x": pos(meta, "Country"), "agg": "sum",
                                     "y": pos(meta, "Sales"), "type": "bar", "top": 3}])
    c = dash["charts"][0]
    for label, n, drill in zip(c["labels"], c["rows"], c["drill"]):
        rows = studio.drill_rows(ds, [drill], limit=500)
        assert rows["matched"] == n, label
    india = studio.drill_rows(ds, [c["drill"][0]])
    assert [r["cells"][3] for r in india["rows"]] == ["₹1,20,000", "30000", "(5,000)"]   # as in the file


def test_filter_that_matches_nothing_shows_nothing(ds):
    meta = studio.open_dataset(ds)
    dash = studio.build(ds, filters=[{"col": pos(meta, "Country"), "op": "in", "values": ["Atlantis"]}])
    assert dash["empty"] is True
    assert dash["matched_rows"] == 0
    assert dash["charts"] == []


def test_filters_change_every_number(ds):
    meta = studio.open_dataset(ds)
    dash = studio.build(ds, filters=[{"col": pos(meta, "Segment"), "op": "in", "values": ["Government"]}])
    k = {x["label"]: x for x in dash["kpis"]}
    assert k["Rows matching filters"]["exact"] == "6"
    gov = Decimal(120000) - 5000 + Decimal("45000.50") + Decimal("2500.25") + 2000 + 4000
    assert Decimal(k["Total Sales"]["exact"]) == gov


def test_date_range_filter(ds):
    meta = studio.open_dataset(ds)
    dash = studio.build(ds, filters=[{"col": pos(meta, "Date"), "op": "between",
                                      "from": "2024-02-01", "to": "2024-03-31"}])
    assert dash["matched_rows"] == 4


def test_column_values_for_filter_picker(ds):
    meta = studio.open_dataset(ds)
    v = studio.column_values(ds, pos(meta, "Discount Band"))
    assert [(x["value"], x["rows"]) for x in v["values"]] == [("Low", 5), ("None", 4), ("High", 3)]


def test_impossible_charts_are_refused_with_a_reason(ds):
    meta = studio.open_dataset(ds)
    with pytest.raises(studio.StudioError, match="not a number column"):
        studio.normalise_chart(meta, {"x": pos(meta, "Segment"), "agg": "sum", "y": pos(meta, "Country")})
    # averages can't be pie slices; becomes a bar
    ch = studio.normalise_chart(meta, {"x": pos(meta, "Segment"), "agg": "avg",
                                       "y": pos(meta, "Sale Price"), "type": "doughnut"})
    assert ch["type"] == "bar"


def test_changing_a_column_setting_changes_the_dashboard(ds):
    meta = studio.open_dataset(ds)
    p = pos(meta, "Sale Price")
    meta = studio.update_settings(ds, {"columns": {str(p): {"agg": "sum"}}})
    assert next(c for c in meta["columns"] if c["pos"] == p)["overridden"]
    k = {x["label"]: x for x in studio.build(ds)["kpis"]}
    assert Decimal(k["Total Sale Price"]["exact"]) == Decimal(sum(range(10, 121, 10)))


# ── Formatting (display only) ────────────────────────────────────────────────

@pytest.mark.parametrize("value, fmt, agg, expected", [
    ("212000.75", "plain", "sum", "212,000.75"),
    ("1234567", "plain", "sum", "1,234,567"),
    ("-5000", "currency:₹", "sum", "-₹5,000"),
    ("0.125", "percent", "avg", "12.5%"),
    ("0.12", "percent", "avg", "12%"),
    ("65", "plain", "avg", "65.00"),
    (None, "plain", "sum", "–"),
])
def test_number_display(value, fmt, agg, expected):
    assert studio.fmt_number(value, fmt, agg) == expected


# ── Old-format uploads ───────────────────────────────────────────────────────

def test_legacy_column_order_is_recovered_from_the_first_chunk():
    keys = ["COGS", "Date", "Sales", "Country", "Gross Sales", "Segment"]   # jsonb key order
    chunk = ("[Sheet1]\n## Sheet1 Segment: Government, Country: Germany, Gross Sales: 529550, "
             "Sales: 529550, COGS: 393380, Date: 2014-12-01 00:00:00")
    assert dataset_store._legacy_column_order(keys, chunk) == \
        ["Segment", "Country", "Gross Sales", "Sales", "COGS", "Date"]
    csv_chunk = "## Data (set_id, name, year, theme) set_id: 1-8, name: Small house set, year: 1970"
    assert dataset_store._legacy_column_order(["year", "name", "theme", "set_id"], csv_chunk) == \
        ["set_id", "name", "year", "theme"]


def test_auto_dashboard_never_groups_by_mixed_or_id_like_columns(engine):
    from tests import messy_files
    sheets, _ = tabular.read_file(messy_files.indian_csv(), "csv")
    a = tabular.analyse_sheet(sheets[0])
    if hasattr(engine, "sql"):
        doc_id = engine.sql("insert into public.documents (name, file_type) values ('v.csv','csv') returning id")[0][0]
    else:
        doc_id = 1
    ds = dataset_store.save_dataset(doc_id, "v.csv", a, sheets[0].percent_cols)
    meta = studio.open_dataset(ds)
    flagged = {c["pos"] for c in meta["columns"] if c["warnings"]}
    for ch in studio.build(ds)["charts"]:
        assert ch["spec"]["x"] not in flagged, ch["title"]
