"""
Smart Search answers for spreadsheet questions, computed exactly.

Before: text chunks were keyword-matched and their LINES counted (a chunk holds
many rows), then the model was told to repeat that number. Answers were wrong
(Germany instead of the United States), and the same question gave 14, 200 or
"not found".

Now:
  1. plan     the model turns the question into a query plan (JSON) using the
              dataset's real column names. It never produces a number.
  2. resolve  the server checks the plan and matches the user's words to the
              values that really occur in each column ("inactive" ->
              "INACTIVE - END SESSION"). Unclear matches are asked about,
              never guessed.
  3. compute  the Data Studio engine (ds_query in Postgres) computes the answer
              exactly.
  4. say      the answer is written from the engine's numbers by code, with a
              "how this was counted" line and the rows behind it.
"""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal

from services import ai_models, dataset_store, studio

log = logging.getLogger(__name__)

AGGS = ("count_rows", "count", "distinct", "sum", "avg", "min", "max")
OPS = ("eq", "in", "not_in", "contains", "gt", "gte", "lt", "lte", "between", "blank", "not_blank")
MAX_GROUPS_SHOWN = 10
MAX_LIST_ROWS = 20
VALUE_HINTS = 25          # distinct values shown to the planner per text column
VALUE_SCAN = 1000         # values checked when matching the user's words


class Clarify(Exception):
    """The question can't be answered exactly without asking the user."""


class NotAPlan(Exception):
    """The model could not turn the question into a query."""


# ── Finding the dataset ──────────────────────────────────────────────────────

def datasets_for(doc_names: list[str]) -> list[dict]:
    """Ready datasets for these documents (one per sheet)."""
    names = set(doc_names)
    return [d for d in dataset_store.list_datasets() if d["document_name"] in names]


# ── 1. Plan ──────────────────────────────────────────────────────────────────

def _schema(meta: dict, hints: dict[int, list[str]]) -> str:
    lines = []
    for c in meta["columns"]:
        s = c["stats"]
        if not s.get("filled"):
            continue
        desc = f'- "{c["name"]}" ({c["type"]}'
        if c["type"] == "number":
            desc += f', default {c["default_agg"]}, {s.get("min")} to {s.get("max")}'
        elif c["type"] == "date":
            desc += f', {s.get("min")} to {s.get("max")}'
        else:
            desc += f', {s.get("distinct", "?")} distinct values'
        desc += ")"
        if hints.get(c["pos"]):
            desc += ": " + ", ".join(json.dumps(v, ensure_ascii=False) for v in hints[c["pos"]])
        lines.append(desc)
    return "\n".join(lines)


PLAN_PROMPT = """You turn a question about a table into a JSON query plan. You never compute or guess numbers.

Table "{label}" with {rows} rows. Columns (type, and for text columns some of the values that occur):
{schema}

Question: {question}

Return ONLY one JSON object:
{{
  "kind": "aggregate" | "list" | "not_about_this_table",
  "measure": {{"agg": "count_rows|count|distinct|sum|avg|min|max", "column": "<column or null for count_rows>"}},
  "group_by": {{"column": "<column>", "grain": "day|month|quarter|year|null"}} or null,
  "order": "desc" | "asc",
  "top": <number of groups wanted, or null for all>,
  "filters": [{{"column": "<column>", "op": "eq|in|not_in|contains|gt|gte|lt|lte|between|blank|not_blank",
               "value": "...", "values": ["..."], "from": "...", "to": "..."}}]
}}

Rules:
- Use column names exactly as listed. Copy text values exactly as listed when one fits.
- "How many" / "count" / "number of" rows -> count_rows. "Highest/lowest X by Y" -> group_by Y,
  measure on X, top 1 with order desc/asc. "Percentage of rows that ..." -> count_rows with the filter.
- Dates: use ISO dates. "in August 2014" -> {{"op":"between","from":"2014-08-01","to":"2014-08-31"}}.
  "in 2014" on a date column -> between 2014-01-01 and 2014-12-31.
- A word like "9th" on a number column means 9.
- One filter per condition in the question, and nothing else. A time period is ONE filter on
  the date column; never also filter a month-name, year or quarter column for the same period.
- "list"/"show"/"which rows" questions -> kind "list" with the filters.
"""


def _value_hints(dataset_id: int, meta: dict) -> dict[int, list[str]]:
    """A few real values of each low-cardinality text column, for the planner."""
    hints = {}
    for c in meta["columns"]:
        if c["type"] != "text" or not c["stats"].get("filled"):
            continue
        top = c["stats"].get("top") or []
        if c["stats"].get("distinct", 0) <= VALUE_HINTS:
            try:
                vals = studio.column_values(dataset_id, c["pos"])["values"]
                hints[c["pos"]] = [v["value"] for v in vals if v["value"] is not None][:VALUE_HINTS]
                continue
            except Exception:
                pass
        hints[c["pos"]] = [t["value"] for t in top]
    return hints


def plan(question: str, meta: dict, hints: dict[int, list[str]]) -> dict:
    label = meta["document_name"] + (f" — {meta['sheet']}" if meta.get("sheet") else "")
    prompt = PLAN_PROMPT.format(label=label, rows=meta["rows"], schema=_schema(meta, hints),
                                question=question)
    text = ai_models.chat_completion([{"role": "user", "content": prompt}],
                                     temperature=0, max_tokens=500)
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        raise NotAPlan("The model did not return a plan.")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise NotAPlan(f"The plan was not valid JSON ({e}).") from e


# ── 2. Resolve the plan against the real data ────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[\s\-_/.,']+", " ", str(s)).strip().casefold()


def _column(meta: dict, name) -> dict:
    if name is None:
        raise Clarify("Which column should I use?")
    want = _norm(name)
    cols = [c for c in meta["columns"] if _norm(c["name"]) == want]
    if not cols:
        cols = [c for c in meta["columns"] if want and want in _norm(c["name"])]
    if len(cols) != 1:
        names = ", ".join(f'"{c["name"]}"' for c in meta["columns"][:12])
        raise Clarify(f'I couldn\'t find a column called "{name}". The columns are: {names}.')
    return cols[0]


def match_value(dataset_id: int, col: dict, word: str) -> list:
    """
    The real value(s) of a text column that the user's word refers to.
    Exact (ignoring case/spacing/punctuation) wins; otherwise a unique
    containing match; several candidates -> Clarify.
    """
    vals = studio.column_values(dataset_id, col["pos"])["values"]   # top 200 by rows
    real = [v["value"] for v in vals if v["value"] is not None]
    w = _norm(word)
    exact = [v for v in real if _norm(v) == w]
    if exact:
        return exact
    partial = [v for v in real if w and w in _norm(v)]
    if len(partial) == 1:
        return partial
    if len(partial) > 1:
        opts = "; ".join(f'"{v}"' for v in partial[:8])
        raise Clarify(f'"{word}" matches more than one {col["name"]}: {opts}. Which one do you mean?')
    # Not among the most common values: check the rest of the column exactly.
    probe = dataset_store.query(dataset_id, [{
        "filters": [{"col": col["pos"], "op": "contains", "value": word}],
        "group_by": {"col": col["pos"]}, "measures": [{"agg": "count_rows"}], "limit": 9}])[0]
    found = [g["g"] for g in probe["groups"] if not g["other"] and g["g"] is not None]
    if len(found) == 1 and probe["group_count"] == 1:
        return found
    if found:
        opts = "; ".join(f'"{v}"' for v in found[:8])
        raise Clarify(f'"{word}" matches more than one {col["name"]}: {opts}. Which one do you mean?')
    common = "; ".join(f'"{v}"' for v in real[:8])
    raise Clarify(f'No row has "{word}" in {col["name"]}. Its most common values are: {common}.')


def _date(text, col_name: str) -> str:
    t = str(text).strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", t):
        raise Clarify(f'I couldn\'t read "{text}" as a date for {col_name}. Please give it as e.g. 2014-08-31.')
    return t


def _number(text, col_name: str) -> str:
    t = re.sub(r"(?<=\d)(st|nd|rd|th)\b", "", str(text).strip())
    try:
        return str(Decimal(t.replace(",", "")))
    except Exception:
        raise Clarify(f'I couldn\'t read "{text}" as a number for {col_name}.')


_PERIOD_WORDS = {"month", "year", "quarter", "qtr", "fy", "week", "day"}


def _drop_repeated_periods(meta: dict, filters: list) -> list:
    """If the plan filters a date column by range, a second filter on a
    'Month Name' / 'Year' column for the same period is redundant, and can
    only lose rows where the two columns disagree. Drop it."""
    from services.tabular import _tokens
    has_range = False
    for f in filters:
        try:
            has_range |= _column(meta, f.get("column"))["type"] == "date" and f.get("op") in ("between", "gte", "lte", "gt", "lt", "eq")
        except Clarify:
            pass
    if not has_range:
        return filters
    keep = []
    for f in filters:
        try:
            c = _column(meta, f.get("column"))
        except Clarify:
            keep.append(f)
            continue
        if c["type"] != "date" and set(_tokens(c["name"])) & _PERIOD_WORDS:
            continue
        keep.append(f)
    return keep


def resolve(dataset_id: int, meta: dict, p: dict) -> dict:
    """Plan -> engine spec, with every column and value checked. Raises Clarify."""
    kind = p.get("kind") or "aggregate"
    p = {**p, "filters": _drop_repeated_periods(meta, p.get("filters") or [])}
    if kind == "not_about_this_table":
        raise NotAPlan("not about this table")

    filters, described = [], []
    for f in p.get("filters") or []:
        col = _column(meta, f.get("column"))
        op = f.get("op") or "eq"
        if op not in OPS:
            raise NotAPlan(f"unknown operator {op}")
        t, pos, name = col["type"], col["pos"], col["name"]
        if op in ("blank", "not_blank"):
            filters.append({"col": pos, "op": op})
            described.append(f"{name} is {'blank' if op == 'blank' else 'not blank'}")
        elif op == "between":
            conv = _date if t == "date" else _number if t == "number" else None
            if conv is None:
                raise Clarify(f"{name} is text, so it can't be a range.")
            lo = conv(f["from"], name) if f.get("from") not in (None, "") else None
            hi = conv(f["to"], name) if f.get("to") not in (None, "") else None
            filters.append({"col": pos, "op": "between", "from": lo, "to": hi})
            described.append(f"{name} from {lo or '…'} to {hi or '…'}")
        elif op in ("gt", "gte", "lt", "lte") or (op == "eq" and t in ("number", "date")):
            conv = _date if t == "date" else _number if t == "number" else None
            if conv is None:
                raise Clarify(f"{name} is text, so it can't be compared as a number.")
            v = conv(f.get("value"), name)
            filters.append({"col": pos, "op": op, "value": v})
            sym = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "eq": "="}[op]
            described.append(f"{name} {sym} {v}")
        elif op == "contains":
            filters.append({"col": pos, "op": "contains", "value": str(f.get("value", ""))})
            described.append(f'{name} contains "{f.get("value")}"')
        else:   # eq / in / not_in on text
            words = f.get("values") if isinstance(f.get("values"), list) and f.get("values") else [f.get("value")]
            vals = []
            for w in words:
                if w in (None, ""):
                    continue
                vals.extend(match_value(dataset_id, col, w))
            if not vals:
                raise Clarify(f"Which {name} do you mean?")
            vals = list(dict.fromkeys(vals))
            filters.append({"col": pos, "op": "not_in" if op == "not_in" else "in", "values": vals})
            shown = " or ".join(f'"{v}"' for v in vals)
            described.append(f"{name} {'is not' if op == 'not_in' else 'is'} {shown}")

    m = p.get("measure") or {}
    agg = m.get("agg") or "count_rows"
    if agg not in AGGS:
        raise NotAPlan(f"unknown aggregation {agg}")
    measure, mcol = {"agg": "count_rows"}, None
    if agg != "count_rows":
        mcol = _column(meta, m.get("column"))
        if agg in ("sum", "avg") and mcol["type"] != "number":
            raise Clarify(f'"{mcol["name"]}" is not a number column, so it can\'t be '
                          f'{"added up" if agg == "sum" else "averaged"}.')
        if agg in ("min", "max") and mcol["type"] == "text":
            raise Clarify(f'"{mcol["name"]}" is text; I can count it but not take a {agg}.')
        measure = {"agg": agg, "col": mcol["pos"]}

    group, gcol = None, None
    g = p.get("group_by")
    if isinstance(g, dict) and g.get("column"):
        gcol = _column(meta, g["column"])
        grain = g.get("grain") if gcol["type"] == "date" and g.get("grain") in studio.GRAINS else None
        if gcol["type"] == "date" and grain is None:
            grain = "month"
        group = {"col": gcol["pos"], "grain": grain}

    order = "asc" if p.get("order") == "asc" else "desc"
    try:
        top = max(1, min(int(p.get("top")), 50)) if p.get("top") is not None else None
    except (TypeError, ValueError):
        top = None
    return {"kind": "list" if kind == "list" else "aggregate", "filters": filters,
            "described": described, "measure": measure, "measure_col": mcol,
            "group": group, "group_col": gcol, "order": order, "top": top}


# ── 3 + 4. Compute and say ───────────────────────────────────────────────────

def _measure_label(r: dict) -> str:
    agg, col = r["measure"]["agg"], r["measure_col"]
    if agg == "count_rows":
        return "number of rows"
    word = {"count": "filled cells of", "distinct": "distinct values of", "sum": "total",
            "avg": "average", "min": "minimum", "max": "maximum"}[agg]
    return f"{word} {col['name']}"


def _fmt(r: dict, v) -> str:
    agg, col = r["measure"]["agg"], r["measure_col"]
    if col and col["type"] == "date" and agg in ("min", "max"):
        return str(v)[:10] if v else "–"
    return studio.fmt_number(v, col["format"] if col else "plain",
                             "count" if agg in ("count_rows", "count", "distinct") else agg)


def compute(dataset_id: int, meta: dict, r: dict) -> dict:
    """Run the resolved plan. Returns {text, rows_matched, filters} for the reply."""
    total_rows = meta["rows"]
    where = (" where " + " and ".join(r["described"])) if r["described"] else ""

    if r["kind"] == "list":
        res = dataset_store.rows(dataset_id, r["filters"], limit=MAX_LIST_ROWS)
        n = res["matched"]
        lines = [f"**{n:,}** of {total_rows:,} rows match{where.replace(' where ', ': ', 1) if where else ''}."]
        show = [c for c in meta["columns"] if c["stats"].get("filled")][:6]
        for row in res["rows"]:
            cells = ", ".join(f'{c["name"]}: {row["cells"][c["pos"]]}' for c in show
                              if row["cells"][c["pos"]] not in (None, ""))
            lines.append(f"- Row {row['row']}: {cells}")
        if n > len(res["rows"]):
            lines.append(f"Showing the first {len(res['rows'])}. Open Data Studio to see all {n:,}.")
        return {"text": "\n".join(lines), "matched": n}

    spec = {"filters": r["filters"], "measures": [r["measure"], {"agg": "count_rows"}]}
    if r["group"]:
        spec["group_by"] = r["group"]
    out = dataset_store.query(dataset_id, [spec])[0]
    n = int(out["total"]["n"])
    label = _measure_label(r)
    how = (f"How this was counted: {label}{where}"
           + (f", grouped by {r['group_col']['name']}" if r["group"] else "")
           + f". {n:,} of {total_rows:,} rows matched.")

    if n == 0:
        return {"text": f"No rows match{where.replace(' where ', ': ', 1)}.\n\n{how}", "matched": 0}

    if not r["group"]:
        v = out["total"]["m"][0]
        if r["measure"]["agg"] == "count_rows":
            pct = (Decimal(n) / Decimal(total_rows) * 100).quantize(Decimal("0.1")) if total_rows else 0
            head = (f"**{n:,}** rows{where} ({pct}% of all {total_rows:,} rows)." if where
                    else f"There are **{n:,}** rows.")
        else:
            head = f"The {label}{where} is **{_fmt(r, v)}**."
        return {"text": f"{head}\n\n{how}", "matched": n}

    groups = [g for g in out["groups"] if g["g"] is not None]
    rev = r["order"] == "desc"
    gtype = r["group_col"]["type"]
    if r["top"] is None and gtype in ("date", "number"):
        # A breakdown over time (or years) reads in order, not ranked.
        groups.sort(key=lambda g: Decimal(g["g"]) if gtype == "number" else g["g"])
    else:
        is_date = r["measure_col"] is not None and r["measure_col"]["type"] == "date"
        conv = (lambda v: v) if is_date else Decimal
        with_val = [g for g in groups if g["m"][0] is not None]
        with_val.sort(key=lambda g: conv(g["m"][0]), reverse=rev)
        groups = with_val + [g for g in groups if g["m"][0] is None]
    top = groups[: (r["top"] or MAX_GROUPS_SHOWN)]
    lead = top[0]
    best = "highest" if rev else "lowest"
    gname = r["group_col"]["name"]
    ties = [g["g"] for g in groups if g["m"][0] == lead["m"][0]]
    if r["top"] == 1:
        if len(ties) > 1:
            head = (f"{len(ties)} {gname} values tie for the {best} {label}{where}: "
                    + ", ".join(f"**{t}**" for t in ties) + f", each **{_fmt(r, lead['m'][0])}**.")
        else:
            head = f"**{lead['g']}** has the {best} {label}{where}: **{_fmt(r, lead['m'][0])}**."
        runners = groups[1:4]
        lines = [head]
        if runners and len(ties) == 1:
            lines.append("Next: " + "; ".join(f"{g['g']} {_fmt(r, g['m'][0])}" for g in runners) + ".")
    else:
        lines = [f"{label[0].upper() + label[1:]} by {gname}{where}:"]
        lab = lambda g: studio._label(g["g"], r["group_col"], r["group"]["grain"])
        lines += [f"- {lab(g)}: **{_fmt(r, g['m'][0])}**" for g in top]
        if len(groups) > len(top):
            lines.append(f"…and {len(groups) - len(top)} more {gname} values.")
    blank = next((g for g in out["groups"] if g["g"] is None), None)
    if blank:
        lines.append(f"({blank['n']:,} row(s) have no {gname} and are not shown.)")
    lines.append("")
    lines.append(how)
    return {"text": "\n".join(lines), "matched": n}


# ── Entry point used by Smart Search ─────────────────────────────────────────

def answer(question: str, doc_names: list[str]) -> dict | None:
    """
    Answer a question about spreadsheet documents exactly.
    Returns {"answer", "sources", "kind"} or None when the question isn't
    about these tables (the caller then uses document search).
    """
    sets = datasets_for(doc_names)
    if not sets:
        return None
    # Several sheets/files: try each; the first whose plan resolves answers.
    last_clarify = None
    for ds in sets:
        meta = studio.open_dataset(ds["id"])
        try:
            p = plan(question, meta, _value_hints(ds["id"], meta))
            r = resolve(ds["id"], meta, p)
            out = compute(ds["id"], meta, r)
        except NotAPlan:
            continue
        except Clarify as c:
            last_clarify = (ds, str(c))
            continue
        src = {"document_name": ds["document_name"], "dataset_id": ds["id"],
               "excerpt": f"Calculated by the exact engine on {meta['rows']:,} rows of "
                          f"{ds['label']}. Open Data Studio to see the rows."}
        return {"answer": out["text"], "sources": [src], "kind": "exact"}
    if last_clarify:
        ds, msg = last_clarify
        return {"answer": msg, "kind": "clarify",
                "sources": [{"document_name": ds["document_name"], "dataset_id": ds["id"],
                             "excerpt": "I need one detail before I can calculate this exactly."}]}
    return None
