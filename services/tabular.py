"""
Reading spreadsheets for Data Studio, accurately and with nothing hidden.

Three rules this module exists to enforce:

  1. Nothing is dropped silently. Every row is either loaded or counted in the
     import report with the reason (blank, above the header, total row).
     Every value that could not be read as its column's type is counted and
     listed with its row number.
  2. Numbers are exact. Values are parsed to Decimal and stored as canonical
     decimal strings; floats are never used for calculation.
  3. Guesses are visible. The header row, each column's type and default
     calculation, and the day/month order of dates are all guesses. They are
     recorded so the review screen can show and override them.

Pure Python, no database access: everything here is unit-testable offline.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

MAX_ROWS   = 200_000      # hard stop with a clear error; never truncate
MAX_SHEETS = 12
UNPARSABLE_TOLERANCE = 0.05   # a column is still a number/date if <=5% can't be read
MIN_ROWS_FOR_TOLERANCE = 20
EXAMPLES = 5

# Cells that mean "no value". Treated as blank and counted in the report.
PLACEHOLDERS = {
    "-", "--", "---", "–", "—", "na", "n/a", "n.a.", "n.a", "nil", "null", "none",
    "nan", "#n/a", "#na", "#value!", "#div/0!", "#ref!", "#name?", "#num!", "#null!",
    "?", ".", "..", "...",
}

_TOTAL_RE = re.compile(r"(grand\s+|sub\s*-?\s*|overall\s+|net\s+)?totals?\s*:?", re.I)


class TabularError(ValueError):
    """A problem with the file that the user needs to fix (shown as-is)."""


# ══ Reading files into a raw grid ════════════════════════════════════════════

@dataclass
class RawSheet:
    name: str
    index: int
    rows: list[tuple[int, list]]              # (row number in file, cells as text or None)
    percent_cols: set[int] = field(default_factory=set)   # Excel cells formatted as %
    notes: list[str] = field(default_factory=list)


def _clean_text(s: str) -> str | None:
    s = s.replace(" ", " ").replace("​", "").replace("﻿", "").strip()
    return s or None


def _float_text(v: float) -> str | None:
    """A float as Excel shows it: 15 significant digits, no exponent."""
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return _dec_text(Decimal(format(v, ".15g")))


def _dec_text(d: Decimal) -> str:
    if d == 0:
        return "0"
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _excel_cell_text(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return _float_text(v)
    if isinstance(v, Decimal):
        return _dec_text(v)
    if isinstance(v, dt.datetime):
        if (v.hour, v.minute, v.second) == (0, 0, 0):
            return v.date().isoformat()
        return v.replace(microsecond=0).isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, dt.time):
        return v.replace(microsecond=0).isoformat()
    if isinstance(v, dt.timedelta):
        return str(v)
    return _clean_text(str(v))


_FORMULA_NO_VALUE = re.compile(rb"<c\b[^>]*?>\s*<f\b[^>]*?(?:/>|>[^<]*</f>)\s*</c>")


def _count_formulas_without_values(file_bytes: bytes) -> int:
    """Formula cells with no saved result (file not last saved by Excel).
    They read as blank, which silently lowers totals, so we count them."""
    n = 0
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            for name in z.namelist():
                if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                    n += len(_FORMULA_NO_VALUE.findall(z.read(name)))
    except Exception:
        return 0
    return n


def _read_xlsx(file_bytes: bytes) -> tuple[list[RawSheet], list[str]]:
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as e:
        raise TabularError(
            "This Excel file could not be opened. If it is an old .xls file, "
            "open it in Excel and save it as .xlsx, then upload again."
        ) from e

    file_notes: list[str] = []
    missing = _count_formulas_without_values(file_bytes)
    if missing:
        file_notes.append(
            f"{missing:,} formula cell(s) have no saved result, so they were read as blank. "
            "Open the file in Excel, save it, and upload again to include them."
        )

    sheets: list[RawSheet] = []
    try:
        for idx, ws in enumerate(wb.worksheets):
            if getattr(ws, "sheet_state", "visible") != "visible":
                file_notes.append(f"Sheet '{ws.title}' is hidden and was not imported.")
                continue
            rows: list[tuple[int, list]] = []
            pct_hits: dict[int, int] = {}
            num_hits: dict[int, int] = {}
            for r_idx, row in enumerate(ws.iter_rows(), start=1):
                cells = []
                for c_idx, cell in enumerate(row):
                    v = cell.value
                    cells.append(_excel_cell_text(v))
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        num_hits[c_idx] = num_hits.get(c_idx, 0) + 1
                        fmt = getattr(cell, "number_format", "") or ""
                        if "%" in fmt:
                            pct_hits[c_idx] = pct_hits.get(c_idx, 0) + 1
                while cells and cells[-1] is None:
                    cells.pop()
                if cells:
                    rows.append((r_idx, cells))
                if len(rows) > MAX_ROWS + 50:
                    raise TabularError(
                        f"Sheet '{ws.title}' has more than {MAX_ROWS:,} rows, which is over the "
                        "Data Studio limit. Nothing was loaded (the file is never cut short)."
                    )
            if not rows:
                continue
            pct_cols = {c for c, n in pct_hits.items() if n * 2 > num_hits.get(c, 0)}
            sheets.append(RawSheet(ws.title, idx, rows, pct_cols))
            if len(sheets) >= MAX_SHEETS:
                file_notes.append(f"Only the first {MAX_SHEETS} sheets were imported.")
                break
    finally:
        wb.close()
    return sheets, file_notes


def _decode_csv(file_bytes: bytes) -> tuple[str, str]:
    if file_bytes[:3] == b"\xef\xbb\xbf":
        return file_bytes[3:].decode("utf-8", errors="replace"), "UTF-8"
    if file_bytes[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return file_bytes.decode("utf-16", errors="replace"), "UTF-16"
    try:
        return file_bytes.decode("utf-8"), "UTF-8"
    except UnicodeDecodeError:
        # Typical of CSVs saved by older Excel on Windows.
        return file_bytes.decode("cp1252", errors="replace"), "Windows-1252"


def _sniff_delimiter(text: str) -> str:
    """Pick the delimiter that gives the most consistent column count (>1)."""
    sample = "\n".join(text.splitlines()[:60])
    best, best_score = ",", -1.0
    for delim in (",", ";", "\t", "|"):
        try:
            widths = [len(r) for r in csv.reader(io.StringIO(sample), delimiter=delim) if r]
        except csv.Error:
            continue
        if not widths:
            continue
        modal = max(set(widths), key=widths.count)
        if modal < 2:
            continue
        score = widths.count(modal) / len(widths) + min(modal, 50) / 1000.0
        if score > best_score:
            best, best_score = delim, score
    return best


_DELIM_NAMES = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}


def _read_csv(file_bytes: bytes) -> tuple[list[RawSheet], list[str]]:
    text, encoding = _decode_csv(file_bytes)
    text = text.replace("\x00", "")
    delim = _sniff_delimiter(text)
    notes = [f"Read as {encoding} text, {_DELIM_NAMES[delim]}-separated."]
    if encoding == "Windows-1252":
        notes.append("The file is not UTF-8; special characters (₹, accents) may look wrong. "
                     "Save as 'CSV UTF-8' in Excel to be safe.")
    csv.field_size_limit(10_000_000)
    rows: list[tuple[int, list]] = []
    for r_idx, rec in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delim), start=1):
        cells = [_clean_text(c) for c in rec]
        while cells and cells[-1] is None:
            cells.pop()
        if cells:
            rows.append((r_idx, cells))
        if len(rows) > MAX_ROWS + 50:
            raise TabularError(
                f"This file has more than {MAX_ROWS:,} rows, which is over the Data Studio "
                "limit. Nothing was loaded (the file is never cut short)."
            )
    return ([RawSheet("", 0, rows)] if rows else []), notes


def read_file(file_bytes: bytes, file_type: str) -> tuple[list[RawSheet], list[str]]:
    """Returns (sheets, file-level notes). Raises TabularError for unusable files."""
    ft = file_type.lower().lstrip(".")
    if ft in ("xlsx", "xlsm"):
        return _read_xlsx(file_bytes)
    if ft == "xls":
        raise TabularError("Old .xls files are not supported. Open the file in Excel, "
                           "save it as .xlsx, and upload again.")
    if ft in ("csv", "tsv", "txt"):
        return _read_csv(file_bytes)
    raise TabularError(f"Data Studio cannot read .{ft} files.")


# ══ Parsing single values ════════════════════════════════════════════════════

_CURRENCY_RE = re.compile(r"^(₹|\$|€|£|¥|rs\.?|inr|usd|eur|gbp)\s*|\s*(₹|\$|€|£|¥|rs\.?|inr|usd|eur|gbp)$", re.I)
_NUM_BODY_RE = re.compile(
    r"(?:"
    r"\d+(?:\.\d*)?"                        # 1234   1234.5   12.
    r"|\.\d+"                               # .5
    r"|\d{1,3}(?:,\d{3})+(?:\.\d*)?"        # 1,234,567.89  (Western grouping)
    r"|\d{1,2}(?:,\d{2})*,\d{3}(?:\.\d*)?"  # 12,34,567.89  (Indian grouping)
    r")(?:[eE][+-]?\d+)?"
)


@dataclass
class Num:
    value: Decimal
    percent: bool = False
    currency: str | None = None
    leading_zero: bool = False


def parse_number(s: str) -> Num | None:
    """
    Strict number parsing. Returns None for anything not unambiguously a number,
    so "1,5", "12 kg" or "N/A" are reported instead of guessed.
    Handles: 1,234.50  12,34,567  (500)  -500  500-  ₹1,200  $40  Rs. 300  12%  1.2E+3
    """
    t = s.replace(" ", " ").replace("−", "-").replace(" ", "").strip()
    if not t:
        return None
    neg = False
    signs = 0                                # more than one sign marker is not a number
    if t.startswith("(") and t.endswith(")"):
        neg, signs, t = True, 1, t[1:-1].strip()
    percent = False
    if t.endswith("%"):
        percent, t = True, t[:-1].strip()
    currency = None
    for _ in range(2):                       # "-₹500" and "₹-500" both occur
        if t.startswith(("-", "+")):
            neg = neg != (t[0] == "-")
            signs += 1
            t = t[1:].strip()
        m = _CURRENCY_RE.search(t)
        if m:
            currency = (m.group(1) or m.group(2)).strip()
            t = (t[:m.start()] + t[m.end():]).strip()
    if t.endswith("-"):                      # trailing minus (SAP-style exports)
        neg, signs, t = not neg, signs + 1, t[:-1].strip()
    if signs > 1 or not t or not _NUM_BODY_RE.fullmatch(t):
        return None
    body = t.replace(",", "")
    try:
        d = Decimal(body)
    except InvalidOperation:
        return None
    int_part = body.split(".")[0].split("e")[0].split("E")[0]
    leading_zero = len(int_part) > 1 and int_part.startswith("0")
    if percent:
        d = d / Decimal(100)
    if neg:
        d = -d
    return Num(d, percent, currency, leading_zero)


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_MONTHS.update({m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)})
_MONTHS["sept"] = 9

_TIME = r"(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?\s*([AaPp][Mm])?\s*(?:Z|[+-]\d{2}:?\d{2})?)?"
_ISO_RE  = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})" + _TIME)
_NUM3_RE = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4}|\d{2})" + _TIME)
_DMON_RE = re.compile(r"(\d{1,2})[-/ .]*([A-Za-z]{3,9})[-/ .,]*(\d{4}|\d{2})" + _TIME)
_MOND_RE = re.compile(r"([A-Za-z]{3,9})[-/ .]*(\d{1,2})(?:st|nd|rd|th)?[-/ .,]+(\d{4})" + _TIME)


@dataclass
class DateParts:
    """A date whose day/month order may still be undecided (a/b/y form)."""
    y: int
    a: int | None = None      # first number  (ambiguous form only)
    b: int | None = None      # second number (ambiguous form only)
    m: int | None = None      # month, when known
    d: int | None = None      # day, when known
    has_time: bool = False
    two_digit_year: bool = False

    def resolve(self, order: str) -> str | None:
        if self.m is not None:
            m, d = self.m, self.d
        elif order == "MDY":
            m, d = self.a, self.b
        else:
            m, d = self.b, self.a
        try:
            return dt.date(self.y, m, d).isoformat()
        except (ValueError, TypeError):
            return None


def _year(txt: str) -> tuple[int, bool]:
    y = int(txt)
    if len(txt) == 2:
        return (2000 + y if y < 50 else 1900 + y), True
    return y, False


def _nonzero_time(m: re.Match, first_group: int) -> bool:
    parts = [m.group(first_group + i) for i in range(3)]
    return any(p and int(p) != 0 for p in parts)


def parse_date(s: str) -> DateParts | None:
    t = s.strip()
    m = _ISO_RE.fullmatch(t)
    if m:
        return DateParts(int(m.group(1)), m=int(m.group(2)), d=int(m.group(3)),
                         has_time=_nonzero_time(m, 4))
    m = _NUM3_RE.fullmatch(t)
    if m:
        y, two = _year(m.group(3))
        return DateParts(y, a=int(m.group(1)), b=int(m.group(2)),
                         has_time=_nonzero_time(m, 4), two_digit_year=two)
    m = _DMON_RE.fullmatch(t)
    if m and m.group(2).lower() in _MONTHS:
        y, two = _year(m.group(3))
        return DateParts(y, m=_MONTHS[m.group(2).lower()], d=int(m.group(1)),
                         has_time=_nonzero_time(m, 4), two_digit_year=two)
    m = _MOND_RE.fullmatch(t)
    if m and m.group(1).lower() in _MONTHS:
        return DateParts(int(m.group(3)), m=_MONTHS[m.group(1).lower()], d=int(m.group(2)),
                         has_time=_nonzero_time(m, 4))
    return None


def is_placeholder(s: str) -> bool:
    return s.strip().lower() in PLACEHOLDERS


# ══ Column analysis ══════════════════════════════════════════════════════════

_ID_TOKENS = {
    "id", "ids", "code", "codes", "no", "num", "number", "sl", "sno", "srno", "serial",
    "pin", "pincode", "zip", "zipcode", "postcode", "phone", "mobile", "contact", "tel",
    "udise", "aadhaar", "aadhar", "account", "acct", "roll", "regn", "registration",
    "year", "yr", "fy", "month", "day", "week", "quarter", "qtr", "lat", "latitude",
    "lon", "long", "lng", "longitude", "rank", "index", "key", "ref", "gstin", "pan",
    "hsn", "sku", "isbn", "barcode", "version",
}
_AVG_TOKENS = {
    "price", "rate", "percent", "percentage", "pct", "avg", "average", "mean", "ratio",
    "score", "rating", "age", "mrp", "gpa", "cgpa", "share", "margin%",
}
# A minimum, maximum, age or level of each row: adding them up means nothing.
_AVG_TOKENS |= {"min", "max", "minimum", "maximum", "level", "grade", "temperature", "temp",
                "weight", "height", "duration", "tenure", "experience", "speed"}
_AVG_PREFIXES = ("age", "avg", "average", "rate", "price")
_COUNT_OF_RE = re.compile(r"^\s*(number|no|nos|num|count|total)\b\.?\s*(of\b)?", re.I)

_MONTH_NAMES = set(_MONTHS)


def _tokens(name: str) -> list[str]:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)
    return [t for t in re.split(r"[^A-Za-z0-9%]+", spaced.lower()) if t]


@dataclass
class Column:
    pos: int
    name: str
    type: str                 # number | date | text
    detected_type: str        # what the app guessed, before any override
    default_agg: str          # sum | avg | count | none
    format: str               # plain | percent | currency:<symbol>
    stats: dict
    warnings: list[str]
    values: list              # canonical value (str) or None, one per data row

    def to_row(self) -> dict:
        return {"pos": self.pos, "name": self.name, "type": self.type,
                "detected_type": self.detected_type, "default_agg": self.default_agg,
                "format": self.format, "stats": self.stats, "warnings": self.warnings}


def _norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _examples(items: list[tuple[int, str]]) -> list[dict]:
    return [{"row": r, "value": v[:60]} for r, v in items[:EXAMPLES]]


def analyse_column(pos: int, name: str, cells: list[tuple[int, str | None]], *,
                   force_type: str | None = None, force_agg: str | None = None,
                   percent_hint: bool = False, date_order: str | None = None) -> Column:
    """
    cells: (file row number, raw text or None) for every data row, in order.
    Looks at EVERY value (not a sample) to decide the type.
    """
    n_rows = len(cells)
    blanks = placeholders = 0
    placeholder_seen: dict[str, int] = {}
    present: list[tuple[int, int, str]] = []          # (index, row number, raw)
    ph_cells: list[tuple[int, int, str]] = []         # "N/A", "-", "None"...
    for i, (rown, raw) in enumerate(cells):
        if raw is None or raw == "":
            blanks += 1
        elif is_placeholder(raw):
            placeholders += 1
            placeholder_seen[raw] = placeholder_seen.get(raw, 0) + 1
            ph_cells.append((i, rown, raw))
        else:
            present.append((i, rown, raw))
    n = len(present)

    nums  = [parse_number(raw) for _, _, raw in present]
    dates = [parse_date(raw) for _, _, raw in present]
    # Keep only shapes that are a real calendar date under at least one order.
    dates = [d if d is not None and (d.resolve("DMY") or d.resolve("MDY")) else None
             for d in dates]
    n_num  = sum(1 for x in nums if x is not None)
    n_date = sum(1 for x in dates if x is not None)

    def enough(hits: int) -> bool:
        if n == 0:
            return False
        if hits == n:
            return True
        return n >= MIN_ROWS_FOR_TOLERANCE and (n - hits) / n <= UNPARSABLE_TOLERANCE

    # Day/month order for a/b/yyyy dates, decided from the whole column.
    ambiguous = [d for d in dates if d is not None and d.m is None]
    first_gt12  = any(d.a > 12 for d in ambiguous)
    second_gt12 = any(d.b > 12 for d in ambiguous)
    order_note = None
    if ambiguous:
        if first_gt12 and second_gt12:
            order, order_note = None, "mixed"
        elif first_gt12:
            order = "DMY"
        elif second_gt12:
            order = "MDY"
        else:
            order, order_note = (date_order or "DMY"), "assumed"
        if date_order in ("DMY", "MDY") and order_note != "mixed":
            if order != date_order and order_note is None:
                order_note = "override_conflict"
            order = date_order
    else:
        order = date_order or "DMY"

    warnings: list[str] = []
    leading_zero = [(rown, raw) for (_, rown, raw), x in zip(present, nums)
                    if x is not None and x.leading_zero]

    if n == 0:
        detected = "text"
    elif enough(n_date) and order_note != "mixed":
        detected = "date"
    elif enough(n_num) and not leading_zero:
        detected = "number"
    else:
        detected = "text"
        if leading_zero and enough(n_num):
            warnings.append(
                f"Kept as text because values have leading zeros (e.g. '{leading_zero[0][1]}'), "
                "which a number would lose. Looks like a code or ID.")
        elif n and n_num / n >= 0.5:
            bad = [(rown, raw) for (_, rown, raw), x in zip(present, nums) if x is None]
            warnings.append(
                f"Mixed column: {n_num:,} numbers and {n - n_num:,} other values "
                f"(e.g. '{bad[0][1][:40]}' in row {bad[0][0]}). Kept as text. "
                "Set the type to Number to calculate with it; the other values would count as blank.")
        elif order_note == "mixed" and n_date == n:
            warnings.append("Dates in this column use both day/month and month/day order, "
                            "so it was kept as text. Fix the source file.")

    ctype = force_type if force_type in ("number", "date", "text") else detected
    values: list = [None] * n_rows
    stats: dict = {"rows": n_rows, "blank": blanks, "filled": n}
    if placeholders:
        stats["placeholders"] = placeholders
        top = sorted(placeholder_seen.items(), key=lambda kv: -kv[1])[:3]
        stats["placeholder_examples"] = [f"{k} ×{v}" for k, v in top]
    fmt = "plain"
    default_agg = "count"

    if ctype == "number":
        unread: list[tuple[int, str]] = []
        total = Decimal(0)
        lo = hi = None
        good = 0
        all_int = True
        distinct: set = set()
        pct = cur = 0
        cur_sym: dict[str, int] = {}
        for (i, rown, raw), x in zip(present, nums):
            if x is None:
                unread.append((rown, raw))
                continue
            v = x.value
            values[i] = _dec_text(v)
            total += v
            lo = v if lo is None or v < lo else lo
            hi = v if hi is None or v > hi else hi
            good += 1
            distinct.add(v)
            if v != v.to_integral_value():
                all_int = False
            if x.percent:
                pct += 1
            if x.currency:
                cur += 1
                cur_sym[x.currency] = cur_sym.get(x.currency, 0) + 1
        if percent_hint:
            fmt = "percent"
        elif good and pct * 2 > good:
            fmt = "percent"
        elif good and cur * 2 > good:
            fmt = "currency:" + max(cur_sym, key=cur_sym.get)
        if good and 0 < pct < good and not percent_hint:
            warnings.append(f"{pct:,} of {good:,} values have a % sign and the rest do not. "
                            "Values with % were divided by 100; check this column.")
        stats.update({
            "unreadable": len(unread), "unreadable_examples": _examples(unread),
            "distinct": len(distinct), "integers_only": all_int,
            "sum": _dec_text(total) if good else None,
            "min": _dec_text(lo) if good else None,
            "max": _dec_text(hi) if good else None,
            "mean": _dec_text((total / good).quantize(Decimal("0.000001"))) if good else None,
        })
        if unread:
            warnings.append(
                f"{len(unread):,} value(s) could not be read as numbers and count as blank "
                f"(e.g. '{unread[0][1][:40]}' in row {unread[0][0]}).")
        default_agg = _guess_number_agg(name, fmt, all_int, len(distinct), good, lo, hi)

    elif ctype == "date":
        unread = []
        lo = hi = None
        distinct = set()
        times = two_digit = 0
        for (i, rown, raw), d in zip(present, dates):
            iso = d.resolve(order or "DMY") if d is not None else None
            if iso is None:
                unread.append((rown, raw))
                continue
            values[i] = iso
            distinct.add(iso)
            lo = iso if lo is None or iso < lo else lo
            hi = iso if hi is None or iso > hi else hi
            times += d.has_time
            two_digit += d.two_digit_year
        stats.update({"unreadable": len(unread), "unreadable_examples": _examples(unread),
                      "distinct": len(distinct), "min": lo, "max": hi})
        if ambiguous:
            stats["date_order"] = order
            stats["date_order_source"] = order_note or "from the data"
            if order_note == "assumed":
                ex = next(raw for (_, _, raw), d in zip(present, dates)
                          if d is not None and d.m is None)
                o = "day/month/year" if order == "DMY" else "month/day/year"
                warnings.append(f"Date order is ambiguous (e.g. '{ex}'). Read as {o}. "
                                "Change the date order if that is wrong.")
        if unread:
            warnings.append(
                f"{len(unread):,} value(s) could not be read as dates and count as blank "
                f"(e.g. '{unread[0][1][:40]}' in row {unread[0][0]}).")
        if times:
            warnings.append(f"{times:,} value(s) include a time of day; only the date is used.")
        if two_digit:
            warnings.append(f"{two_digit:,} date(s) have 2-digit years; 00-49 read as 20xx, 50-99 as 19xx.")
        default_agg = "none"

    else:  # text
        # In a text column "None", "N/A" or "-" are real labels (e.g. a
        # Discount Band of "None"), exactly as Excel and Power BI show them.
        # They only count as blank in number and date columns.
        if ph_cells:
            stats["filled"] = n + len(ph_cells)
            stats.pop("placeholders", None)
            stats.pop("placeholder_examples", None)
        counts: dict[str, int] = {}
        respaced = 0
        for i, rown, raw in sorted(present + ph_cells):
            v = _norm_text(raw)
            if v != raw:
                respaced += 1
            values[i] = v
            counts[v] = counts.get(v, 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:EXAMPLES]
        stats.update({"distinct": len(counts),
                      "top": [{"value": k[:60], "count": c} for k, c in top]})
        folded: dict[str, set] = {}
        for v in counts:
            folded.setdefault(v.casefold(), set()).add(v)
        variants = [sorted(vs) for vs in folded.values() if len(vs) > 1]
        if variants:
            stats["case_variants"] = len(variants)
            a, b = variants[0][0], variants[0][1]
            warnings.append(
                f"{len(variants):,} value(s) differ only by capital letters (e.g. '{a[:30]}' and "
                f"'{b[:30]}'). They are counted as separate groups.")
        if respaced:
            stats["extra_spaces_removed"] = respaced
        if counts and all(k.lower() in _MONTH_NAMES for k in counts):
            stats["month_names"] = True
        default_agg = "count"

    if force_agg in ("sum", "avg", "count", "none"):
        if not (force_agg in ("sum", "avg") and ctype != "number"):
            default_agg = force_agg

    return Column(pos, name, ctype, detected, default_agg, fmt, stats, warnings, values)


def _guess_number_agg(name: str, fmt: str, all_int: bool, distinct: int, good: int,
                      lo: Decimal | None, hi: Decimal | None) -> str:
    """sum, avg or none (identifier-like). A guess, always shown for review."""
    toks = _tokens(name)
    counting = bool(_COUNT_OF_RE.match(name)) and len(toks) > 1
    if fmt == "percent" or "%" in name or any(t in _AVG_TOKENS for t in toks) \
            or any(t.startswith(_AVG_PREFIXES) for t in toks) \
            or re.search(r"\bper\b", name, re.I):
        return "avg"
    if not counting and any(t in _ID_TOKENS for t in toks):
        return "none"
    if all_int and good >= MIN_ROWS_FOR_TOLERANCE and distinct == good:
        return "none"          # every value different: a serial number or ID
    if all_int and lo is not None and lo >= 1900 and hi <= 2100 and distinct <= 250 and good > distinct:
        return "none"          # looks like years
    return "sum"


# ══ Whole-sheet analysis ═════════════════════════════════════════════════════

@dataclass
class Analysed:
    sheet_name: str
    sheet_index: int
    header_row: int                    # file row number of the header; 0 = no header row
    columns: list[Column]
    records: list[dict]                # {"row_num", "status", "raw": [...], "val": [...] | None}
    report: dict

    @property
    def data_rows(self) -> int:
        return sum(1 for r in self.records if r["status"] == "data")


def _looks_like_label(cell: str | None) -> bool:
    return cell is not None and parse_number(cell) is None and parse_date(cell) is None


def detect_header_row(rows: list[tuple[int, list]]) -> int:
    """
    File row number of the header: the first row, among the first 30, that is
    mostly filled with text labels and is about as wide as the data below it.
    Returns 0 when no row looks like a header.
    """
    if not rows:
        return 0
    head = rows[:30]
    widths = sorted(sum(1 for c in cells if c is not None) for _, cells in rows[:200])
    typical = widths[len(widths) // 2]
    need = 1 if typical <= 1 else max(2, -(-typical * 6 // 10))   # ceil(60%)
    for i, (rown, cells) in enumerate(head):
        filled = [c for c in cells if c is not None]
        if len(filled) < need or i + 1 >= len(rows):
            continue
        if all(_looks_like_label(c) for c in filled) and len(set(filled)) >= max(1, len(filled) - 1):
            return rown
    return 0


def _unique_names(raw_names: list[str | None]) -> tuple[list[str], list[str]]:
    names, notes, seen = [], [], {}
    for i, h in enumerate(raw_names):
        base = _norm_text(h) if h else ""
        if not base:
            base = f"Column {i + 1}"
            notes.append(f"Column {i + 1} has no header; named '{base}'.")
        k = base.casefold()
        seen[k] = seen.get(k, 0) + 1
        if seen[k] > 1:
            new = f"{base} ({seen[k]})"
            notes.append(f"Two columns are named '{base}'; the later one was renamed '{new}'.")
            base = new
        names.append(base)
    return names, notes


def analyse_sheet(sheet: RawSheet, options: dict | None = None) -> Analysed:
    """
    options (all optional; these are the user's overrides from the review screen):
      header_row     int   file row number of the header (0 = file has no header)
      date_order     "DMY" | "MDY"
      include_totals bool  keep rows that look like totals
      columns        {"<pos>": {"type": ..., "agg": ..., "date_order": ...}}
    """
    options = options or {}
    rows = sheet.rows
    if not rows:
        raise TabularError("The sheet is empty.")

    hdr = options.get("header_row")
    header_row = detect_header_row(rows) if hdr is None else int(hdr)
    if header_row and header_row not in {rn for rn, _ in rows}:
        header_row = detect_header_row(rows)

    width = max(len(cells) for rn, cells in rows if rn >= header_row)
    header_cells: list = []
    above: list[tuple[int, list]] = []
    body: list[tuple[int, list]] = []
    for rn, cells in rows:
        if header_row and rn < header_row:
            above.append((rn, cells))
        elif header_row and rn == header_row:
            header_cells = cells
        else:
            body.append((rn, cells))
    if not body:
        raise TabularError("No data rows were found below the header row.")

    names, name_notes = _unique_names(
        [header_cells[i] if i < len(header_cells) else None for i in range(width)])
    if not header_row:
        name_notes = ["No header row was found; columns are named Column 1, Column 2, …"]

    include_totals = bool(options.get("include_totals"))
    data: list[tuple[int, list]] = []
    totals: list[tuple[int, list]] = []
    for rn, cells in body:
        padded = cells + [None] * (width - len(cells))
        if any(c is not None and _TOTAL_RE.fullmatch(c) for c in padded):
            totals.append((rn, padded))
            if include_totals:
                data.append((rn, padded))
        else:
            data.append((rn, padded))
    if not data:
        raise TabularError("Every row below the header looks like a total row.")

    col_opts = options.get("columns") or {}
    columns: list[Column] = []
    for pos in range(width):
        o = col_opts.get(str(pos)) or {}
        columns.append(analyse_column(
            pos, names[pos], [(rn, cells[pos]) for rn, cells in data],
            force_type=o.get("type"), force_agg=o.get("agg"),
            percent_hint=pos in sheet.percent_cols,
            date_order=o.get("date_order") or options.get("date_order")))

    records: list[dict] = []
    for rn, cells in above:
        records.append({"row_num": rn, "status": "above_header", "raw": cells, "val": None})
    if header_row:
        records.append({"row_num": header_row, "status": "header", "raw": header_cells, "val": None})
    data_index = {rn: i for i, (rn, _) in enumerate(data)}
    for rn, padded in sorted(data + ([] if include_totals else totals), key=lambda x: x[0]):
        if rn in data_index:
            i = data_index[rn]
            records.append({"row_num": rn, "status": "data", "raw": padded,
                            "val": [c.values[i] for c in columns]})
        else:
            records.append({"row_num": rn, "status": "total", "raw": padded, "val": None})

    seen_rows: set = set()
    dupes = 0
    for rn, cells in data:
        key = tuple(cells)
        if key in seen_rows:
            dupes += 1
        seen_rows.add(key)

    last = rows[-1][0]
    blank_rows = last - len(rows)
    warnings: list[dict] = []

    def warn(level: str, msg: str):
        warnings.append({"level": level, "msg": msg})

    if above:
        first_text = next((c for c in above[0][1] if c), "")
        warn("info", f"{len(above)} row(s) above the header were skipped "
                     f"(row {above[0][0]} starts with '{first_text[:50]}'). "
                     "Change the header row if this is wrong.")
    for nnote in name_notes:
        warn("info", nnote)
    if totals and not include_totals:
        ex = next(c for c in totals[0][1] if c is not None and _TOTAL_RE.fullmatch(c))
        warn("warn", f"{len(totals)} row(s) look like totals and were left out so nothing is "
                     f"counted twice (row {totals[0][0]}: '{ex}'). Turn on 'Include total rows' to keep them.")
    if totals and include_totals:
        warn("warn", f"{len(totals)} row(s) that look like totals are included, as you chose. "
                     "Sums may be counted twice.")
    if dupes:
        warn("warn", f"{dupes:,} row(s) are exact copies of another row. They are kept and counted.")
    for c in columns:
        for w in c.warnings:
            warn("warn", f"{c.name}: {w}")
    empty_cols = [c.name for c in columns if c.stats["filled"] == 0]
    if empty_cols:
        warn("info", f"{len(empty_cols)} column(s) have no values: {', '.join(empty_cols[:5])}"
                     + ("…" if len(empty_cols) > 5 else "") + ".")

    report = {
        "sheet": sheet.name,
        "header_row": header_row,
        "rows": {
            "last_row_in_file": last,
            "non_blank": len(rows),
            "blank_skipped": blank_rows,
            "above_header": len(above),
            "header": 1 if header_row else 0,
            "total_rows": len(totals),
            "totals_included": include_totals,
            "loaded": len(data),
            "duplicates": dupes,
        },
        "columns": len(columns),
        "warnings": warnings,
        # Every non-blank row is accounted for: this must always be true.
        "reconciles": len(rows) == len(above) + (1 if header_row else 0)
                      + len(data) + (0 if include_totals else len(totals)),
    }
    return Analysed(sheet.name, sheet.index, header_row, columns, records, report)


def import_file(file_bytes: bytes, file_type: str) -> tuple[list[Analysed], list[str]]:
    """Read a file and analyse every sheet. Returns (sheets, file-level notes)."""
    sheets, notes = read_file(file_bytes, file_type)
    out: list[Analysed] = []
    for sh in sheets:
        try:
            out.append(analyse_sheet(sh))
        except TabularError as e:
            label = f"Sheet '{sh.name}'" if sh.name else "The file"
            notes.append(f"{label} was not imported: {e}")
    if not out:
        raise TabularError("No table was found in this file. " + " ".join(notes))
    return out, notes
