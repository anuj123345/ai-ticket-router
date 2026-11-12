"""
Deliberately messy spreadsheets, built in code so the tests need no binary
fixtures. Each one packs in the problems real MIS/finance exports have.
The expected answers in the tests are worked out by hand from these rows.
"""
import datetime as dt
import io
import random
from decimal import Decimal

import openpyxl


def sales_xlsx() -> bytes:
    """
    Sheet 'Sales':
      row 1  title, row 2 blank, row 3 subtitle, row 4 header, rows 5-12 data,
      row 13 blank, row 14 'Grand Total'.
      Problems: title rows, duplicate header ('Amount' twice), %-formatted
      column, real Excel dates, 'N/A' in a number column, blank region cell,
      'east' vs 'East', leading-zero codes stored as text, a Year column.
    Sheet 'Targets': a second small table.
    Sheet 'Scratch': hidden.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Quarterly Sales Report"])
    ws.append([])
    ws.append(["Generated 01-Apr-2024", None, "Confidential"])
    ws.append(["Region", "Store Code", "Date", "Year", "Units", "Amount", "Discount %", "Amount", "Notes"])
    rows = [
        ["East",  "0012", dt.datetime(2024, 1, 15), 2024, 10, 1000.10, 0.10, 5,     "ok"],
        ["East",  "0013", dt.datetime(2024, 1, 31), 2024, 20, 2000.20, 0.20, 6,     None],
        ["West",  "0101", dt.datetime(2024, 2, 1),  2024, 30, 3000.30, 0.00, 7,     "late"],
        ["west",  "0102", dt.datetime(2024, 2, 29), 2024, 40, "N/A",   0.05, 8,     None],
        [None,    "0200", dt.datetime(2024, 3, 10), 2024, 50, 5000.50, 0.15, 9,     None],
        ["North", "0300", dt.datetime(2023, 12, 31), 2023, 60, 6000.60, 0.25, 10,   None],
        ["North", "0301", dt.datetime(2024, 4, 1),  2024, 70, 0.1,     0.50, 11,    None],
        ["North", "0302", dt.datetime(2024, 4, 2),  2024, 80, 0.2,     0.75, 12,    None],
    ]
    for r in rows:
        ws.append(r)
    for r in range(5, 13):
        ws.cell(row=r, column=7).number_format = "0%"
    ws.append([])
    ws.append(["Grand Total", None, None, None, 360, 17001.0, None, 68])

    ws2 = wb.create_sheet("Targets")
    ws2.append(["Region", "Target"])
    ws2.append(["East", 5000])
    ws2.append(["West", 7000])
    ws2.append(["North", 9000])

    ws3 = wb.create_sheet("Scratch")
    ws3.append(["x", "y"])
    ws3.append([1, 2])
    ws3.sheet_state = "hidden"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def indian_csv() -> bytes:
    """
    Semicolon-separated, Windows-1252, Indian number grouping, rupee amounts
    as 'Rs.', bracket and trailing-minus negatives, % as text, dd/mm/yyyy
    dates (day > 12 proves the order), a blank line, placeholders, a Total row.
    """
    lines = [
        "District;Scheme;Sanctioned (Rs.);Spent;Utilisation;Paid On;Beneficiaries",
        "Khordha;Mid Day Meal;Rs. 12,34,567.50;10,00,000;81%;25/03/2024;1,250",
        "Cuttack;Mid Day Meal;Rs. 8,00,000;(50,000);-;31/03/2024;900",
        "Puri;Uniforms;Rs. 5,50,000.25;2,75,000.25-;50%;01/04/2024;NA",
        "",
        "Ganjam;Uniforms;Rs. 20,00,000;19,99,999.99;100%;15/04/2024;2,000",
        "Khordha;Uniforms;Rs. 1,00,000;café;25%;02/04/2024;150",
        "Total;;Rs. 46,84,567.75;;;;4,300",
    ]
    return "\r\n".join(lines).encode("cp1252")


def ambiguous_dates_csv() -> bytes:
    """Every date has day <= 12, so day/month order cannot be known from the data."""
    return (b"Invoice,Date,Amount\n"
            b"A1,03/04/2024,100\n"
            b"A2,05/06/2024,200\n"
            b"A3,07/08/2024,300\n")


def big_random_rows(n: int = 3000, seed: int = 7):
    """Header + rows for a large CSV with awkward decimals, blanks and many groups."""
    rnd = random.Random(seed)
    cities = [f"City {i:02d}" for i in range(40)] + ["Ünïcode Town", "O'Brien"]
    cats = ["A", "B", "C", "D", None]
    start = dt.date(2021, 11, 20)
    rows = []
    for i in range(n):
        amount = Decimal(rnd.randrange(-500000, 5000000)) / Decimal(1000)   # 3 decimals
        qty = rnd.randrange(0, 50)
        rate = Decimal(rnd.randrange(0, 10001)) / Decimal(10000)
        d = start + dt.timedelta(days=rnd.randrange(0, 900))
        rows.append([
            f"ID{i:05d}",
            rnd.choice(cities) if rnd.random() > 0.03 else None,
            rnd.choice(cats),
            d.isoformat() if rnd.random() > 0.02 else None,
            format(amount, "f") if rnd.random() > 0.04 else None,
            str(qty),
            format(rate, "f"),
        ])
    return ["Order ID", "City", "Category", "Order Date", "Amount", "Qty", "Rate"], rows


def big_random_csv(n: int = 3000, seed: int = 7) -> bytes:
    import csv
    header, rows = big_random_rows(n, seed)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if c is None else c for c in r])
    return buf.getvalue().encode("utf-8")
