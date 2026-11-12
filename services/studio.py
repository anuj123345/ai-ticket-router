"""
Data Studio: dashboards built on the exact engine (ds_query in Postgres).

This module never adds numbers up. It decides WHAT to calculate (which
columns, which aggregation, which grouping), asks the database, and formats
the exact answers for display. Every chart reports whether its parts add up
to its total, and every bar/slice carries the filter that shows the rows
behind it.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from decimal import Decimal, ROUND_HALF_UP

from services import ai_models, dataset_store

log = logging.getLogger(__name__)

AGGS = ("count_rows", "count", "distinct", "sum", "avg", "min", "max")
CHART_TYPES = ("bar", "bar_v", "doughnut", "line")
GRAINS = ("day", "month", "quarter", "year")
FILTER_OPS = ("in", "not_in", "gt", "gte", "lt", "lte", "eq", "ne", "between",
              "contains", "blank", "not_blank")
MAX_CHARTS = 8
MAX_LINE_POINTS = 400
VALUE_LIST_LIMIT = 200

AGG_LABEL = {"count_rows": "Count of rows", "count": "Count of", "distinct": "Distinct count of",
             "sum": "Sum of", "avg": "Average of", "min": "Minimum of", "max": "Maximum of"}

_BAR = ["#6366f1", "#8b5cf6", "#a855f7", "#c084fc", "#d8b4fe", "#7c3aed", "#4f46e5", "#818cf8",
        "#6d28d9", "#5b21b6", "#4338ca", "#a78bfa"]
_DONUT = ["#6366f1", "#06b6d4", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#ec4899", "#14b8a6",
          "#f97316", "#84cc16", "#0ea5e9", "#a855f7"]
_OTHER = "#94a3b8"
_BLANK = "#cbd5e1"


class StudioError(ValueError):
    """Bad request from the page (unknown column, wrong aggregation...)."""


# ── Datasets ─────────────────────────────────────────────────────────────────

def list_all() -> list[dict]:
    """Datasets ready to open, plus older uploads that convert on first open."""
    items = [{"id": d["id"], "label": d["label"], "rows": d["row_count"], "legacy": False}
             for d in dataset_store.list_datasets()]
    for d in dataset_store.legacy_documents():
        items.append({"id": None, "document_id": d["document_id"], "label": d["name"],
                      "rows": None, "legacy": True})
    return sorted(items, key=lambda x: x["label"].lower())


def open_dataset(dataset_id: int | None = None, document_id: int | None = None) -> dict:
    """Metadata for the page. Converts an old-format document on first open."""
    if dataset_id is None:
        if document_id is None:
            raise StudioError("dataset_id or document_id required")
        existing = [d for d in dataset_store.list_datasets() if d["document_id"] == document_id]
        dataset_id = existing[0]["id"] if existing else dataset_store.convert_legacy(document_id)
    ds = dataset_store.get_dataset(dataset_id)
    return _public_meta(ds)


def _public_meta(ds: dict) -> dict:
    cols = [{
        "pos": c["pos"], "name": c["name"], "type": c["type"],
        "detected_type": c["detected_type"], "default_agg": c["default_agg"],
        "format": c["format"], "stats": c["stats"], "warnings": c["warnings"],
        "overridden": c["type"] != c["detected_type"]
                      or str(c["pos"]) in ((ds.get("options") or {}).get("columns") or {}),
    } for c in ds["columns"]]
    report = ds.get("report") or {}
    return {
        "id": ds["id"], "document_name": ds["document_name"], "sheet": ds["sheet_name"],
        "rows": ds["row_count"], "header_row": ds["header_row"],
        "options": {k: v for k, v in (ds.get("options") or {}).items() if k != "percent_cols"},
        "columns": cols, "report": report,
        "warning_count": sum(1 for w in report.get("warnings", []) if w.get("level") == "warn"),
    }


def update_settings(dataset_id: int, changes: dict) -> dict:
    clean: dict = {}
    if "header_row" in changes:
        hr = changes["header_row"]
        if hr is not None and (not isinstance(hr, int) or hr < 0):
            raise StudioError("Header row must be a row number (0 = no header row).")
        clean["header_row"] = hr
    if "date_order" in changes:
        if changes["date_order"] not in (None, "DMY", "MDY"):
            raise StudioError("Date order must be DMY or MDY.")
        clean["date_order"] = changes["date_order"]
    if "include_totals" in changes:
        clean["include_totals"] = bool(changes["include_totals"])
    if "columns" in changes:
        cols = {}
        for pos, o in (changes["columns"] or {}).items():
            o = o or {}
            entry = {}
            if o.get("type") not in (None, "auto", "number", "date", "text"):
                raise StudioError(f"Unknown type {o.get('type')!r}")
            if o.get("agg") not in (None, "auto", "sum", "avg", "count", "none"):
                raise StudioError(f"Unknown calculation {o.get('agg')!r}")
            if o.get("date_order") not in (None, "auto", "DMY", "MDY"):
                raise StudioError("Date order must be DMY or MDY.")
            for k in ("type", "agg", "date_order"):
                if k in o:
                    entry[k] = o[k]
            cols[str(int(pos))] = entry
        clean["columns"] = cols
    dataset_store.rebuild_dataset(dataset_id, clean)
    return _public_meta(dataset_store.get_dataset(dataset_id))


# ── Formatting ───────────────────────────────────────────────────────────────

def _dec(x) -> Decimal | None:
    if x is None:
        return None
    try:
        return Decimal(str(x))
    except Exception:
        return None


def _group_digits(int_text: str) -> str:
    neg = int_text.startswith("-")
    t = int_text.lstrip("-")
    parts = []
    while len(t) > 3:
        parts.insert(0, t[-3:])
        t = t[:-3]
    parts.insert(0, t)
    return ("-" if neg else "") + ",".join(parts)


def fmt_number(x, col_format: str = "plain", agg: str = "sum") -> str:
    """Exact value -> display text. Rounds only for display, never in maths."""
    d = _dec(x)
    if d is None:
        return "–"
    if col_format == "percent" and agg in ("sum", "avg", "min", "max"):
        p = (d * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        whole, _, frac = format(p, "f").partition(".")
        return _group_digits(whole) + ("." + frac if frac and frac != "0" else "") + "%"
    places = Decimal("1") if d == d.to_integral_value() and agg != "avg" else Decimal("0.01")
    q = d.quantize(places, rounding=ROUND_HALF_UP)
    s = format(q, "f")
    whole, _, frac = s.partition(".")
    out = _group_digits(whole) + ("." + frac if frac else "")
    if col_format.startswith("currency:") and agg in ("sum", "avg", "min", "max"):
        sym = col_format.split(":", 1)[1]
        sym = {"rs": "₹", "rs.": "₹", "inr": "₹", "usd": "$", "eur": "€", "gbp": "£"}.get(sym.lower(), sym)
        out = ("-" + sym + out[1:]) if out.startswith("-") else sym + out
    return out


def _short(x) -> str:
    d = _dec(x)
    if d is None:
        return "–"
    a = abs(d)
    for size, suffix in ((Decimal(10) ** 9, "B"), (Decimal(10) ** 6, "M"), (Decimal(10) ** 3, "K")):
        if a >= size:
            return f"{(d / size).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}{suffix}"
    return fmt_number(d)


def _label(g, x_col: dict, grain: str | None) -> str:
    if g is None:
        return "(Blank)"
    if x_col["type"] == "date" and grain == "month":
        try:
            return dt.date(int(g[:4]), int(g[5:7]), 1).strftime("%b %Y")
        except ValueError:
            return g
    return g


# ── Charts ───────────────────────────────────────────────────────────────────

def _cols(meta: dict) -> dict[int, dict]:
    return {c["pos"]: c for c in meta["columns"]}


def _auto_grain(col: dict) -> str:
    lo, hi = col["stats"].get("min"), col["stats"].get("max")
    try:
        span = (dt.date.fromisoformat(hi[:10]) - dt.date.fromisoformat(lo[:10])).days
    except (TypeError, ValueError):
        return "month"
    if span <= 62:
        return "day"
    if span <= 3 * 366:
        return "month"
    if span <= 8 * 366:
        return "quarter"
    return "year"


def normalise_chart(meta: dict, cfg: dict) -> dict:
    """Validate a chart request against the dataset's columns. Raises StudioError."""
    cols = _cols(meta)
    try:
        x = int(cfg["x"])
    except (KeyError, TypeError, ValueError):
        raise StudioError("Each chart needs a column to group by.")
    if x not in cols:
        raise StudioError(f"Unknown column {x}.")
    xc = cols[x]
    agg = cfg.get("agg") or "count_rows"
    if agg not in AGGS:
        raise StudioError(f"Unknown calculation {agg!r}.")
    y = cfg.get("y")
    if agg == "count_rows":
        y = None
    else:
        try:
            y = int(y)
        except (TypeError, ValueError):
            raise StudioError(f"{AGG_LABEL[agg]} needs a column.")
        if y not in cols:
            raise StudioError(f"Unknown column {y}.")
        yt = cols[y]["type"]
        if agg in ("sum", "avg") and yt != "number":
            raise StudioError(f"'{cols[y]['name']}' is not a number column, so it can't be "
                              f"{'summed' if agg == 'sum' else 'averaged'}. Change its type in Columns.")
        if agg in ("min", "max") and yt == "text":
            raise StudioError(f"'{cols[y]['name']}' is text; use Count or Distinct count.")
    ctype = cfg.get("type") or "bar"
    if ctype not in CHART_TYPES:
        ctype = "bar"
    ordered_x = xc["type"] in ("date", "number")
    if ctype == "line" and not ordered_x:
        ctype = "bar_v"
    if ctype == "doughnut" and agg not in ("count_rows", "count", "sum"):
        ctype = "bar"            # slices must add up to a whole; averages don't
    grain = None
    if xc["type"] == "date":
        grain = cfg.get("grain") if cfg.get("grain") in GRAINS else _auto_grain(xc)
    top = cfg.get("top")
    try:
        top = max(3, min(int(top), 50))
    except (TypeError, ValueError):
        top = 6 if ctype == "doughnut" else 10
    return {"x": x, "grain": grain, "agg": agg, "y": y, "type": ctype, "top": top}


def _title(meta: dict, ch: dict) -> str:
    cols = _cols(meta)
    xn = cols[ch["x"]]["name"] + (f" ({ch['grain']})" if ch["grain"] else "")
    if ch["agg"] == "count_rows":
        return f"Count of rows by {xn}"
    return f"{AGG_LABEL[ch['agg']]} {cols[ch['y']]['name']} by {xn}"


def _measure(ch: dict) -> dict:
    return {"agg": "count_rows"} if ch["agg"] == "count_rows" else {"agg": ch["agg"], "col": ch["y"]}


def _chart_spec(meta: dict, ch: dict, filters: list) -> dict:
    spec = {"filters": filters, "group_by": {"col": ch["x"], "grain": ch["grain"]},
            "measures": [_measure(ch)]}
    xc = _cols(meta)[ch["x"]]
    ordered = xc["type"] in ("date", "number")
    many = xc["stats"].get("distinct", 0) > (MAX_LINE_POINTS if ch["type"] == "line" else 50)
    # Ordered axes (dates, years) show every point in order; categories show
    # the top N and one "Other" group computed from the remaining rows.
    if not ordered or many:
        spec["limit"] = ch["top"] if ch["type"] != "line" else MAX_LINE_POINTS
    return spec


def _sort_key(g, xtype):
    if g is None:
        return (1, 0, "")
    if xtype == "number":
        d = _dec(g)
        return (0, d if d is not None else Decimal(0), g)
    return (0, 0, g)


def _render_chart(meta: dict, ch: dict, result: dict, idx: int, filters: list) -> dict:
    cols = _cols(meta)
    xc = cols[ch["x"]]
    yfmt = cols[ch["y"]]["format"] if ch["y"] is not None else "plain"
    groups = result["groups"]
    if ch["type"] == "line" or (xc["type"] in ("date", "number") and ch["type"] != "doughnut"
                                and not any(g["other"] for g in groups)):
        groups = sorted(groups, key=lambda g: (g["other"], _sort_key(g["g"], xc["type"])))
    if ch["type"] == "line":
        groups = [g for g in groups if g["g"] is not None]   # a line has no "(Blank)" point
    shown_keys = [g["g"] for g in groups if not g["other"]]

    labels, values, display, colors, drill = [], [], [], [], []
    other_count = result["group_count"] - len(shown_keys)
    for i, g in enumerate(groups):
        v = g["m"][0]
        if g["other"]:
            labels.append(f"Other ({other_count:,} more)")
            drill.append({"col": ch["x"], "grain": ch["grain"], "op": "not_in", "values": shown_keys})
        else:
            labels.append(_label(g["g"], xc, ch["grain"]))
            drill.append({"col": ch["x"], "grain": ch["grain"], "op": "in", "values": [g["g"]]})
        d = _dec(v)
        values.append(float(d) if d is not None else None)
        display.append(fmt_number(v, yfmt, ch["agg"]))
        palette = _DONUT if ch["type"] in ("doughnut", "bar_v") else _BAR
        colors.append(_OTHER if g["other"] else _BLANK if g["g"] is None else palette[i % len(palette)])

    total = result["total"]["m"][0]
    check = None
    if ch["agg"] in ("count_rows", "count", "sum"):
        parts = sum((_dec(g["m"][0]) or Decimal(0)) for g in result["groups"])
        tot = _dec(total) or Decimal(0)
        dropped = sum((_dec(g["m"][0]) or Decimal(0)) for g in result["groups"]
                      if ch["type"] == "line" and g["g"] is None)
        ok = parts == tot
        check = {"ok": ok, "total": fmt_number(total, yfmt, ch["agg"]),
                 "text": (f"Adds up to the total: {fmt_number(total, yfmt, ch['agg'])}" if ok else
                          f"Parts ({fmt_number(parts, yfmt, ch['agg'])}) do not match the total "
                          f"({fmt_number(total, yfmt, ch['agg'])})")}
        if dropped:
            check["text"] += f" (incl. {fmt_number(dropped, yfmt, ch['agg'])} with no date, not drawn)"
    elif ch["agg"] == "avg":
        check = {"ok": True, "total": fmt_number(total, yfmt, "avg"),
                 "text": f"Overall average: {fmt_number(total, yfmt, 'avg')}"
                         + (" · 'Other' is the average of its own rows" if other_count > 0 else "")}

    chart = {
        "id": f"chart{idx + 1}", "type": "doughnut" if ch["type"] == "doughnut" else
                                        "line" if ch["type"] == "line" else "bar",
        "indexAxis": "y" if ch["type"] == "bar" else "x",
        "title": _title(meta, ch), "spec": ch,
        "labels": labels, "display": display, "drill": drill,
        "rows": [g["n"] for g in groups],
        "check": check, "group_count": result["group_count"],
    }
    if ch["type"] == "line":
        chart["datasets"] = [{"label": chart["title"], "data": values, "borderColor": "#8b5cf6",
                              "backgroundColor": "rgba(139,92,246,0.12)", "tension": 0.25,
                              "fill": True, "pointRadius": 3, "pointBackgroundColor": "#8b5cf6"}]
    elif ch["type"] == "doughnut":
        chart["datasets"] = [{"data": values, "backgroundColor": colors,
                              "borderWidth": 2, "borderColor": "#ffffff"}]
    else:
        chart["datasets"] = [{"label": chart["title"], "data": values,
                              "backgroundColor": colors, "borderRadius": 4}]
    return chart


def auto_charts(meta: dict) -> list[dict]:
    """A sensible starting dashboard from the column profile. No AI involved."""
    cols = meta["columns"]
    numbers = [c for c in cols if c["type"] == "number" and c["default_agg"] in ("sum", "avg")]
    dates = [c for c in cols if c["type"] == "date" and c["stats"].get("distinct", 0) > 1]
    def good_dim(c):
        s = c["stats"]
        distinct, filled = s.get("distinct", 0), s.get("filled", 0)
        if c["type"] != "text" or not 2 <= distinct <= 60:
            return False
        if c["warnings"]:                    # mixed numbers/text, codes with leading zeros
            return False
        return not (filled > 3 and distinct == filled)   # a different value on every row: an ID
    dims = [c for c in cols if good_dim(c)]
    dims.sort(key=lambda c: (c["stats"].get("distinct", 0) > 12, c["pos"]))
    # Low-cardinality whole-number columns that aren't summed (Year, Month Number)
    num_dims = [c for c in cols if c["type"] == "number" and c["default_agg"] == "none"
                and c["stats"].get("integers_only") and 2 <= c["stats"].get("distinct", 0) <= 40]

    main = next((c for c in numbers if c["default_agg"] == "sum"), numbers[0] if numbers else None)
    measure = ({"agg": main["default_agg"], "y": main["pos"]} if main else {"agg": "count_rows", "y": None})

    out: list[dict] = []
    if dates:
        out.append({"x": dates[0]["pos"], "type": "line", **measure})
    elif num_dims and main:
        out.append({"x": num_dims[0]["pos"], "type": "bar_v", **measure})
    for i, d in enumerate(dims[:3]):
        out.append({"x": d["pos"], "type": "doughnut" if i == 1 and measure["agg"] != "avg" else "bar",
                    **measure})
    if main and dims:
        out.append({"x": dims[0]["pos"], "type": "bar", "agg": "count_rows", "y": None})
    second = next((c for c in numbers if main and c["pos"] != main["pos"]), None)
    if second and dims:
        out.append({"x": dims[0]["pos"], "type": "bar", "agg": second["default_agg"], "y": second["pos"]})
    if not out:
        first_text = next((c for c in cols if c["type"] == "text" and c["stats"].get("filled")), None)
        if first_text:
            out.append({"x": first_text["pos"], "type": "bar", "agg": "count_rows", "y": None})
    return out[:6]


# ── Filters ──────────────────────────────────────────────────────────────────

def normalise_filters(meta: dict, filters) -> list[dict]:
    cols = _cols(meta)
    out = []
    for f in filters or []:
        if not isinstance(f, dict):
            raise StudioError("Bad filter.")
        try:
            pos = int(f.get("col"))
        except (TypeError, ValueError):
            raise StudioError("Filter needs a column.")
        if pos not in cols:
            raise StudioError(f"Unknown column {pos}.")
        op = f.get("op")
        if op not in FILTER_OPS:
            raise StudioError(f"Unknown filter {op!r}.")
        clean = {"col": pos, "op": op}
        if f.get("grain") in GRAINS and cols[pos]["type"] == "date":
            clean["grain"] = f["grain"]
        if op in ("in", "not_in"):
            vals = f.get("values")
            if not isinstance(vals, list):
                raise StudioError("Filter values must be a list.")
            clean["values"] = [None if v is None else str(v) for v in vals][:1000]
        elif op == "between":
            clean["from"] = None if f.get("from") in (None, "") else str(f["from"])
            clean["to"] = None if f.get("to") in (None, "") else str(f["to"])
        elif op not in ("blank", "not_blank"):
            clean["value"] = str(f.get("value", ""))
        out.append(clean)
    return out


def filter_label(meta: dict, f: dict) -> str:
    c = _cols(meta)[f["col"]]["name"]
    op = f["op"]
    if op in ("in", "not_in"):
        vals = ["(Blank)" if v is None else v for v in f["values"]]
        shown = ", ".join(vals[:3]) + (f" +{len(vals) - 3}" if len(vals) > 3 else "")
        return f"{c} {'is' if op == 'in' else 'is not'} {shown}"
    if op == "between":
        return f"{c} {f.get('from') or '…'} to {f.get('to') or '…'}"
    if op in ("blank", "not_blank"):
        return f"{c} is {'blank' if op == 'blank' else 'not blank'}"
    sym = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "eq": "=", "ne": "≠", "contains": "contains"}[op]
    return f"{c} {sym} {f['value']}"


# ── Dashboard ────────────────────────────────────────────────────────────────

def build(dataset_id: int, filters=None, charts=None) -> dict:
    meta = open_dataset(dataset_id)
    cols = _cols(meta)
    flt = normalise_filters(meta, filters)
    requested = charts if charts else auto_charts(meta)
    chart_cfgs, errors = [], []
    for cfg in requested[:MAX_CHARTS]:
        try:
            chart_cfgs.append(normalise_chart(meta, cfg))
        except StudioError as e:
            errors.append(str(e))

    numbers = [c for c in meta["columns"] if c["type"] == "number" and c["default_agg"] in ("sum", "avg")][:4]
    kpi_measures = [{"agg": "count_rows"}]
    for c in numbers:
        kpi_measures += [{"agg": c["default_agg"], "col": c["pos"]}, {"agg": "count", "col": c["pos"]}]

    specs = [{"filters": flt, "measures": kpi_measures}]
    specs += [_chart_spec(meta, ch, flt) for ch in chart_cfgs]
    results = dataset_store.query(dataset_id, specs)

    totals = results[0]["total"]
    matched = int(totals["n"])
    all_rows = meta["rows"]
    kpis = [{"label": "Rows" + (" matching filters" if flt else ""), "value": fmt_number(matched, "plain", "count"),
             "exact": str(matched), "sub": f"of {fmt_number(all_rows, 'plain', 'count')} loaded" if flt else
             "loaded from the file"}]
    for i, c in enumerate(numbers):
        v, filled = totals["m"][1 + 2 * i], int(totals["m"][2 + 2 * i])
        missing = matched - filled
        kpis.append({
            "label": f"{'Total' if c['default_agg'] == 'sum' else 'Average'} {c['name']}",
            "value": fmt_number(v, c["format"], c["default_agg"]),
            "short": _short(v) if c["format"] == "plain" or c["format"].startswith("currency") else None,
            "exact": v,
            "sub": f"{missing:,} row(s) blank, not counted" if missing else f"all {filled:,} rows",
        })

    out = {
        "dataset": {"id": meta["id"], "label": meta["document_name"] + (f" — {meta['sheet']}" if meta["sheet"] else ""),
                    "rows": all_rows, "warning_count": meta["warning_count"]},
        "matched_rows": matched, "empty": matched == 0 and bool(flt),
        "filters": [{**f, "label": filter_label(meta, f)} for f in flt],
        "kpis": kpis, "charts": [], "errors": errors,
    }
    if out["empty"]:
        out["message"] = "No rows match these filters. Remove a filter to see data."
        return out
    for i, (ch, res) in enumerate(zip(chart_cfgs, results[1:])):
        out["charts"].append(_render_chart(meta, ch, res, i, flt))
    return out


def column_values(dataset_id: int, pos: int, filters=None, grain: str | None = None) -> dict:
    """Values of one column with their row counts (for filter pickers)."""
    meta = open_dataset(dataset_id)
    cols = _cols(meta)
    if pos not in cols:
        raise StudioError(f"Unknown column {pos}.")
    flt = normalise_filters(meta, filters)
    col = cols[pos]
    if col["type"] == "date" and grain not in GRAINS:
        grain = "month"
    elif col["type"] != "date":
        grain = None
    res = dataset_store.query(dataset_id, [{
        "filters": flt, "group_by": {"col": pos, "grain": grain},
        "measures": [{"agg": "count_rows"}], "limit": VALUE_LIST_LIMIT}])[0]
    vals = [{"value": g["g"], "label": _label(g["g"], col, grain), "rows": g["n"]}
            for g in res["groups"] if not g["other"]]
    if col["type"] in ("date", "number"):
        vals.sort(key=lambda v: _sort_key(v["value"], col["type"]))
    else:
        vals.sort(key=lambda v: (v["value"] is None, -v["rows"], (v["value"] or "").lower()))
    return {"column": col["name"], "type": col["type"], "grain": grain, "values": vals,
            "more": max(0, res["group_count"] - len(vals)),
            "min": col["stats"].get("min"), "max": col["stats"].get("max")}


def drill_rows(dataset_id: int, filters=None, limit: int = 100, offset: int = 0) -> dict:
    meta = open_dataset(dataset_id)
    flt = normalise_filters(meta, filters)
    res = dataset_store.rows(dataset_id, flt, limit=max(1, min(int(limit), 500)), offset=max(0, int(offset)))
    return {"columns": [c["name"] for c in meta["columns"]], "matched": res["matched"],
            "rows": res["rows"], "filters": [filter_label(meta, f) for f in flt]}


# ── AI suggestions (columns and calculations only; never numbers) ─────────────

def suggest(dataset_id: int) -> list[dict]:
    meta = open_dataset(dataset_id)
    lines = []
    for c in meta["columns"]:
        s = c["stats"]
        extra = f", {s.get('distinct', '?')} distinct values"
        if c["type"] == "text" and s.get("top"):
            extra += ", e.g. " + ", ".join(t["value"] for t in s["top"][:3])
        lines.append(f'- "{c["name"]}": {c["type"]}, default {c["default_agg"]}{extra}')
    prompt = f"""You design dashboards. Suggest 4-6 charts for this table ({meta['rows']} rows).

Columns:
{chr(10).join(lines)}

Return ONLY a JSON array. Each item:
{{"x": "<column to group by>", "agg": "count_rows|sum|avg|count|distinct|min|max",
  "y": "<column to calculate, or null for count_rows>", "type": "bar|bar_v|doughnut|line"}}

Rules: use column names exactly as written. sum/avg only on number columns whose
default is sum/avg. Group by text columns with few distinct values, or by dates
(type line). Never group by an ID-like column. Prefer counts by status/category
columns, and totals of the main amounts by the main categories."""
    text = ai_models.chat_completion([{"role": "user", "content": prompt}],
                                     temperature=0.1, max_tokens=700)
    m = re.search(r"\[[\s\S]*\]", text)
    if not m:
        return []
    by_name = {c["name"].strip().lower(): c["pos"] for c in meta["columns"]}
    out = []
    for item in json.loads(m.group(0))[:8]:
        try:
            cfg = {"x": by_name.get(str(item.get("x", "")).strip().lower()),
                   "agg": item.get("agg") or "count_rows",
                   "y": by_name.get(str(item.get("y") or "").strip().lower()),
                   "type": item.get("type") or "bar"}
            out.append(normalise_chart(meta, cfg))
        except StudioError:
            continue
    return out[:6]
