"""
Data Engine — reads structured rows from dataset_rows, detects column types,
computes KPIs, and builds Chart.js-compatible chart configs for Data Studio.
"""
from __future__ import annotations
import re
from models.db import get_client

MONTH_ORDER = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

QUARTER_MAP = {
    "Q1": ["January", "February", "March"],
    "Q2": ["April", "May", "June"],
    "Q3": ["July", "August", "September"],
    "Q4": ["October", "November", "December"],
}


# ── Data fetching ─────────────────────────────────────────────────────────────

def get_data_documents() -> list[dict]:
    """Return all documents that have structured row data."""
    client = get_client()
    try:
        resp = (client.table("dataset_rows")
                .select("document_name")
                .execute())
        names = sorted({r["document_name"] for r in (resp.data or [])})
        return [{"name": n} for n in names]
    except Exception:
        return []


def _fetch_rows(document_name: str) -> list[dict]:
    """Paginated fetch of all rows for a document."""
    client = get_client()
    all_rows = []
    offset = 0
    while True:
        resp = (client.table("dataset_rows")
                .select("row_data")
                .eq("document_name", document_name)
                .range(offset, offset + 999)
                .execute())
        batch = resp.data or []
        all_rows.extend(r["row_data"] for r in batch)
        if len(batch) < 1000:
            break
        offset += 1000
    return all_rows


# ── Column type detection ─────────────────────────────────────────────────────

def _to_float(v) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError, AttributeError):
        return None


def _detect_columns(rows: list[dict]) -> dict[str, str]:
    """Classify each column as numeric / month / date / categorical / empty."""
    if not rows:
        return {}
    all_keys = list(rows[0].keys())
    col_types: dict[str, str] = {}

    for col in all_keys:
        values = [r.get(col) for r in rows if r.get(col) is not None
                  and str(r.get(col)).strip() != ""]
        if not values:
            col_types[col] = "empty"
            continue

        sample = values[:min(50, len(values))]

        # Numeric check
        num_hits = sum(1 for v in sample if _to_float(v) is not None)
        if num_hits / len(sample) > 0.8:
            col_types[col] = "numeric"
            continue

        # Month name check
        month_hits = sum(1 for v in sample
                         if str(v).strip().capitalize() in MONTH_ORDER)
        if month_hits / len(sample) > 0.5:
            col_types[col] = "month"
            continue

        # ISO date check
        date_hits = sum(1 for v in sample
                        if re.match(r"\d{4}-\d{2}-\d{2}", str(v)))
        if date_hits / len(sample) > 0.5:
            col_types[col] = "date"
            continue

        col_types[col] = "categorical"

    return col_types


# ── Aggregation helpers ───────────────────────────────────────────────────────

def _format_value(n: float) -> str:
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:.0f}"


def _agg_by_cat(rows: list[dict], cat_col: str, num_col: str,
                top_n: int = 8) -> tuple[list[str], list[float]]:
    """Sum num_col grouped by cat_col, return top N sorted descending."""
    agg: dict[str, float] = {}
    for row in rows:
        k = str(row.get(cat_col) or "").strip()
        v = _to_float(row.get(num_col))
        if k and v is not None:
            agg[k] = agg.get(k, 0.0) + v
    top = sorted(agg.items(), key=lambda x: x[1], reverse=True)[:top_n]
    return [i[0] for i in top], [round(i[1], 2) for i in top]


def _agg_by_month(rows: list[dict], month_col: str,
                  num_col: str) -> tuple[list[str], list[float]]:
    """Sum num_col grouped by month name, sorted by calendar order."""
    agg: dict[str, float] = {}
    for row in rows:
        raw = str(row.get(month_col) or "").strip()
        k = raw.capitalize()
        if k not in MONTH_ORDER:
            # Try extracting from ISO date
            m = re.match(r"\d{4}-(\d{2})-\d{2}", raw)
            if m:
                idx = int(m.group(1)) - 1
                k = MONTH_ORDER[idx] if 0 <= idx < 12 else raw
        v = _to_float(row.get(num_col))
        if k in MONTH_ORDER and v is not None:
            agg[k] = agg.get(k, 0.0) + v
    ordered_keys = [m for m in MONTH_ORDER if m in agg]
    return ordered_keys, [round(agg[k], 2) for k in ordered_keys]


# ── Quarter filter ────────────────────────────────────────────────────────────

def _filter_by_quarter(rows: list[dict], date_col: str,
                       quarter: str) -> list[dict]:
    target_months = set(QUARTER_MAP.get(quarter, []))
    if not target_months:
        return rows
    filtered = []
    for row in rows:
        raw = str(row.get(date_col) or "").strip()
        k = raw.capitalize()
        if k in target_months:
            filtered.append(row)
            continue
        m = re.match(r"\d{4}-(\d{2})-\d{2}", raw)
        if m:
            month_name = MONTH_ORDER[int(m.group(1)) - 1]
            if month_name in target_months:
                filtered.append(row)
    return filtered or rows


# ── Chart colour palettes ─────────────────────────────────────────────────────

_BAR_COLORS = [
    "#6366f1", "#8b5cf6", "#a78bfa", "#c4b5fd",
    "#7c3aed", "#4f46e5", "#818cf8", "#ddd6fe",
]
_DONUT_COLORS = [
    "#6366f1", "#8b5cf6", "#ec4899", "#f59e0b",
    "#10b981", "#3b82f6", "#f97316", "#14b8a6",
]
_LINE_COLOR = "#8b5cf6"


# ── Main builder ──────────────────────────────────────────────────────────────

def build_dashboard(document_name: str, quarter: str | None = None) -> dict:
    """
    Returns full dashboard config:
    { kpis, charts, has_quarters, total_rows, columns, document_name }
    """
    rows = _fetch_rows(document_name)
    if not rows:
        return {"error": "No structured data found for this document."}

    col_types = _detect_columns(rows)
    numeric_cols  = [c for c, t in col_types.items() if t == "numeric"]
    cat_cols      = [c for c, t in col_types.items() if t == "categorical"]
    month_cols    = [c for c, t in col_types.items() if t in ("month", "date")]

    # Quarter filter
    time_col = month_cols[0] if month_cols else None
    if quarter and time_col:
        rows = _filter_by_quarter(rows, time_col, quarter)

    total_rows = len(rows)

    # ── KPI cards ────────────────────────────────────────────────────────────
    kpis = []
    for col in numeric_cols[:3]:
        total = sum(_to_float(r.get(col)) or 0 for r in rows)
        kpis.append({
            "label": f"Sum of {col}",
            "value": _format_value(total),
            "raw":   round(total, 2),
        })
    if not kpis:
        kpis.append({"label": "Total Records", "value": str(total_rows), "raw": total_rows})

    # ── Charts ───────────────────────────────────────────────────────────────
    charts = []

    # Chart 1: Horizontal bar — cat[0] vs num[0]
    if cat_cols and numeric_cols:
        labels, values = _agg_by_cat(rows, cat_cols[0], numeric_cols[0])
        charts.append({
            "id": "chart1", "type": "bar",
            "title": f"Sum of {numeric_cols[0]} by {cat_cols[0]}",
            "labels": labels,
            "datasets": [{
                "label": numeric_cols[0],
                "data":  values,
                "backgroundColor": _BAR_COLORS[:len(values)],
                "borderRadius": 4,
            }],
            "indexAxis": "y",
        })

    # Chart 2: Doughnut — cat[1] (or cat[0]) vs num[0]
    donut_cat = cat_cols[1] if len(cat_cols) > 1 else (cat_cols[0] if cat_cols else None)
    if donut_cat and numeric_cols:
        labels, values = _agg_by_cat(rows, donut_cat, numeric_cols[0], top_n=6)
        charts.append({
            "id": "chart2", "type": "doughnut",
            "title": f"Sum of {numeric_cols[0]} by {donut_cat}",
            "labels": labels,
            "datasets": [{
                "data": values,
                "backgroundColor": _DONUT_COLORS[:len(values)],
                "borderWidth": 2,
                "borderColor": "#ffffff",
            }],
        })

    # Chart 3: Horizontal bar — cat[0] vs num[1]
    if cat_cols and len(numeric_cols) >= 2:
        labels, values = _agg_by_cat(rows, cat_cols[0], numeric_cols[1])
        charts.append({
            "id": "chart3", "type": "bar",
            "title": f"Sum of {numeric_cols[1]} by {cat_cols[0]}",
            "labels": labels,
            "datasets": [{
                "label": numeric_cols[1],
                "data":  values,
                "backgroundColor": _BAR_COLORS[:len(values)],
                "borderRadius": 4,
            }],
            "indexAxis": "y",
        })

    # Chart 4: Doughnut — cat[2] vs num[1] (multi-series style)
    if len(cat_cols) >= 2 and len(numeric_cols) >= 2:
        labels2, vals_n0 = _agg_by_cat(rows, cat_cols[0], numeric_cols[0], top_n=5)
        _, vals_n1 = _agg_by_cat(rows, cat_cols[0], numeric_cols[1], top_n=5)
        charts.append({
            "id": "chart4", "type": "doughnut",
            "title": f"Sum of {numeric_cols[0]} and {numeric_cols[1]} by {cat_cols[0]}",
            "labels": labels2,
            "datasets": [
                {
                    "label": numeric_cols[0],
                    "data": vals_n0,
                    "backgroundColor": _DONUT_COLORS[:len(labels2)],
                    "borderWidth": 2,
                    "borderColor": "#ffffff",
                },
            ],
        })

    # Chart 5: Vertical bar — cat[2] or cat[1] vs num[0] (top customers style)
    bar_cat = cat_cols[2] if len(cat_cols) > 2 else (cat_cols[1] if len(cat_cols) > 1 else None)
    if bar_cat and numeric_cols:
        labels, values = _agg_by_cat(rows, bar_cat, numeric_cols[0], top_n=6)
        charts.append({
            "id": "chart5", "type": "bar",
            "title": f"Sum of {numeric_cols[0]} by {bar_cat}",
            "labels": labels,
            "datasets": [{
                "label": numeric_cols[0],
                "data":  values,
                "backgroundColor": _DONUT_COLORS[:len(values)],
                "borderRadius": 4,
            }],
            "indexAxis": "x",
        })

    # Chart 6: Line — month/date col vs num[0]
    if time_col and numeric_cols:
        m_labels, m_values = _agg_by_month(rows, time_col, numeric_cols[0])
        charts.append({
            "id": "chart6", "type": "line",
            "title": f"Sum of {numeric_cols[0]} by {time_col}",
            "labels": m_labels,
            "datasets": [{
                "label": numeric_cols[0],
                "data":  m_values,
                "borderColor":     _LINE_COLOR,
                "backgroundColor": "rgba(139,92,246,0.12)",
                "tension": 0.4,
                "fill": True,
                "pointBackgroundColor": _LINE_COLOR,
                "pointRadius": 4,
            }],
            "indexAxis": "x",
        })

    return {
        "document_name": document_name,
        "total_rows":    total_rows,
        "kpis":          kpis,
        "charts":        charts,
        "has_quarters":  bool(time_col),
        "columns":       col_types,
    }
