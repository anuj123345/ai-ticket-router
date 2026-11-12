"""
Reading messy files: every expected number below is worked out by hand from
tests/messy_files.py, not by the code under test.
"""
from decimal import Decimal

import pytest

from services import tabular
from tests import messy_files


def col(a, name):
    return next(c for c in a.columns if c.name == name)


# ── Excel ────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def sales():
    sheets, notes = tabular.import_file(messy_files.sales_xlsx(), "xlsx")
    return sheets, notes


def test_all_visible_sheets_are_imported_and_hidden_one_is_reported(sales):
    sheets, notes = sales
    assert [s.sheet_name for s in sheets] == ["Sales", "Targets"]
    assert any("Scratch" in n and "hidden" in n for n in notes)


def test_header_is_found_below_title_rows(sales):
    a = sales[0][0]
    assert a.header_row == 4
    assert [c.name for c in a.columns] == [
        "Region", "Store Code", "Date", "Year", "Units", "Amount",
        "Discount %", "Amount (2)", "Notes"]


def test_every_row_is_accounted_for(sales):
    r = sales[0][0].report
    assert r["rows"] == {
        "last_row_in_file": 14, "non_blank": 12, "blank_skipped": 2,
        "above_header": 2, "header": 1, "total_rows": 1, "totals_included": False,
        "loaded": 8, "duplicates": 0}
    assert r["reconciles"] is True


def test_grand_total_row_is_excluded_so_nothing_is_counted_twice(sales):
    a = sales[0][0]
    assert col(a, "Units").stats["sum"] == "360"          # not 720
    assert [r["status"] for r in a.records].count("total") == 1


def test_number_column_with_na_is_summed_exactly(sales):
    amount = col(sales[0][0], "Amount")
    assert amount.type == "number"
    assert amount.stats["placeholders"] == 1              # the 'N/A'
    assert amount.stats["filled"] == 7
    # 1000.10 + 2000.20 + 3000.30 + 5000.50 + 6000.60 + 0.1 + 0.2, exactly
    assert amount.stats["sum"] == "17002"
    assert amount.default_agg == "sum"


def test_floats_do_not_leak_rounding_noise(sales):
    a = sales[0][0]
    vals = [r["val"][5] for r in a.records if r["status"] == "data"]
    assert "0.1" in vals and "0.2" in vals
    assert Decimal(vals[-2]) + Decimal(vals[-1]) == Decimal("0.3")


def test_percent_format_column_averages_instead_of_summing(sales):
    d = col(sales[0][0], "Discount %")
    assert d.format == "percent"
    assert d.default_agg == "avg"
    assert d.stats["mean"] == "0.25"                      # (10+20+0+5+15+25+50+75)/8 = 25%


def test_codes_with_leading_zeros_stay_text(sales):
    code = col(sales[0][0], "Store Code")
    assert code.type == "text"
    assert "0012" in [r["val"][1] for r in sales[0][0].records if r["status"] == "data"]


def test_year_column_is_not_summed_by_default(sales):
    year = col(sales[0][0], "Year")
    assert year.type == "number" and year.default_agg == "none"


def test_excel_dates_are_real_dates(sales):
    d = col(sales[0][0], "Date")
    assert d.type == "date"
    assert (d.stats["min"], d.stats["max"]) == ("2023-12-31", "2024-04-02")


def test_capitalisation_variants_are_flagged_not_merged(sales):
    region = col(sales[0][0], "Region")
    assert region.stats["distinct"] == 4                  # East, West, west, North
    assert region.stats["blank"] == 1
    assert any("capital letters" in w for w in region.warnings)


def test_duplicate_header_is_renamed_and_reported(sales):
    msgs = [w["msg"] for w in sales[0][0].report["warnings"]]
    assert any("Two columns are named 'Amount'" in m for m in msgs)
    assert col(sales[0][0], "Amount (2)").stats["sum"] == "68"


# ── CSV with Indian formats ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def indian():
    sheets, notes = tabular.import_file(messy_files.indian_csv(), "csv")
    return sheets[0], notes


def test_semicolon_and_windows_encoding_are_detected(indian):
    a, notes = indian
    assert "semicolon" in notes[0] and "Windows-1252" in notes[0]
    assert len(a.columns) == 7
    assert a.report["rows"]["loaded"] == 5
    assert a.report["rows"]["blank_skipped"] == 1
    assert a.report["rows"]["total_rows"] == 1


def test_indian_grouping_and_rupee_amounts_sum_exactly(indian):
    s = col(indian[0], "Sanctioned (Rs.)")
    assert s.type == "number"
    assert s.format == "currency:Rs."
    # 12,34,567.50 + 8,00,000 + 5,50,000.25 + 20,00,000 + 1,00,000
    assert s.stats["sum"] == "4684567.75"


def test_unreadable_value_is_listed_with_its_row(indian):
    spent = col(indian[0], "Spent")
    # 4 of 5 are numbers (80%): below the bar, so kept as text and flagged.
    assert spent.type == "text"
    assert any("Mixed column" in w and "café" in w and "row 7" in w for w in spent.warnings)


def test_forcing_number_type_counts_the_bad_value_as_blank(indian):
    sheets, _ = tabular.read_file(messy_files.indian_csv(), "csv")
    a = tabular.analyse_sheet(sheets[0], {"columns": {"3": {"type": "number"}}})
    spent = col(a, "Spent")
    # 10,00,000 + (50,000) + 2,75,000.25- + 19,99,999.99
    assert spent.stats["sum"] == "2674999.74"
    assert spent.stats["unreadable"] == 1
    assert spent.stats["unreadable_examples"] == [{"row": 7, "value": "café"}]


def test_percent_text_and_dash_placeholder(indian):
    u = col(indian[0], "Utilisation")
    assert u.type == "number" and u.format == "percent" and u.default_agg == "avg"
    assert u.stats["placeholders"] == 1
    assert u.stats["mean"] == "0.64"                      # (81+50+100+25)/4


def test_day_month_order_is_proven_by_the_data(indian):
    d = col(indian[0], "Paid On")
    assert d.type == "date"
    assert d.stats["date_order"] == "DMY"
    assert d.stats["date_order_source"] == "from the data"
    assert (d.stats["min"], d.stats["max"]) == ("2024-03-25", "2024-04-15")


def test_thousands_separators_in_counts(indian):
    b = col(indian[0], "Beneficiaries")
    assert b.stats["sum"] == "4300" and b.stats["placeholders"] == 1


# ── Ambiguous dates ──────────────────────────────────────────────────────────

def test_ambiguous_date_order_is_flagged_and_can_be_overridden():
    sheets, _ = tabular.read_file(messy_files.ambiguous_dates_csv(), "csv")
    a = tabular.analyse_sheet(sheets[0])
    d = col(a, "Date")
    assert d.stats["date_order_source"] == "assumed"
    assert d.stats["min"] == "2024-04-03"                 # read as 3 April
    assert any("ambiguous" in w for w in d.warnings)

    b = tabular.analyse_sheet(sheets[0], {"date_order": "MDY"})
    assert col(b, "Date").stats["min"] == "2024-03-04"    # read as 4 March


# ── Value parsing ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("1,234.50", "1234.50"), ("12,34,567", "1234567"), ("(500)", "-500"),
    ("500-", "-500"), ("₹1,200", "1200"), ("-$40.5", "-40.5"), ("12%", "0.12"),
    ("1.2E+3", "1200"), ("  42 ", "42"), ("−7", "-7"),
])
def test_numbers_that_must_parse(text, expected):
    assert tabular.parse_number(text).value == Decimal(expected)


@pytest.mark.parametrize("text", ["1,5", "12 kg", "1,234,56", "1234,567", "abc", "", "1.2.3", "--5"])
def test_things_that_must_not_be_guessed_as_numbers(text):
    assert tabular.parse_number(text) is None


def test_xls_gets_a_clear_message():
    with pytest.raises(tabular.TabularError, match="save it as .xlsx"):
        tabular.read_file(b"\xd0\xcf\x11\xe0", "xls")


def test_file_with_no_header_row():
    sheets, _ = tabular.read_file(b"1,2,3\n4,5,6\n7,8,9\n", "csv")
    a = tabular.analyse_sheet(sheets[0])
    assert a.header_row == 0
    assert [c.name for c in a.columns] == ["Column 1", "Column 2", "Column 3"]
    assert a.report["rows"]["loaded"] == 3
    assert col(a, "Column 1").stats["sum"] == "12"


def test_number_of_students_is_summed_but_student_id_is_not():
    csv = b"Student ID,Number of Students,No. of Teachers\n" + b"".join(
        f"{1000 + i},{i % 7},{i % 3}\n".encode() for i in range(30))
    sheets, _ = tabular.read_file(csv, "csv")
    a = tabular.analyse_sheet(sheets[0])
    assert col(a, "Student ID").default_agg == "none"
    assert col(a, "Number of Students").default_agg == "sum"
    assert col(a, "No. of Teachers").default_agg == "sum"


def test_placeholder_words_are_real_labels_in_text_columns():
    """Financial Sample's Discount Band has a real category called "None"."""
    rows = [(1, ["Discount Band", "Sales"])]
    bands = ["None", "Low", "None", "High", "N/A"]
    for i, b in enumerate(bands, start=2):
        rows.append((i, [b, "-" if b == "N/A" else str(i * 10)]))
    a = tabular.analyse_sheet(tabular.RawSheet("", 0, rows))
    band, sales = a.columns
    assert band.type == "text"
    assert band.values == bands                         # nothing turned into blank
    assert band.stats["filled"] == 5
    assert sales.type == "number"
    assert sales.values[4] is None                      # "-" is blank in a number column
    assert sales.stats["placeholders"] == 1
