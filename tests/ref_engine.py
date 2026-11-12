"""
An independent, deliberately simple re-implementation of ds_query() in pure
Python with Decimal. It shares no code with the SQL engine.

Two jobs:
  * the answer key: tests run the same query through Postgres and through this,
    and the results must match exactly;
  * a database stand-in, so dashboard tests can run with no Postgres at all.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, getcontext

getcontext().prec = 60


def _dim(types, row, pos, grain=None):
    v = row[pos]
    if v is None or v == "":
        return None
    if types[pos] == "date":
        d = dt.date.fromisoformat(v[:10])
        g = grain or "day"
        if g == "day":
            return d.isoformat()
        if g == "month":
            return f"{d.year:04d}-{d.month:02d}"
        if g == "quarter":
            return f"{d.year:04d}-Q{(d.month - 1) // 3 + 1}"
        if g == "year":
            return f"{d.year:04d}"
        raise ValueError(g)
    return v


def _val(types, row, pos):
    v = row[pos]
    if v is None or v == "":
        return None
    if types[pos] == "number":
        return Decimal(v)
    if types[pos] == "date":
        return dt.date.fromisoformat(v[:10])
    return v


def _cast(types, pos, text):
    if types[pos] == "number":
        return Decimal(text)
    if types[pos] == "date":
        return dt.date.fromisoformat(text)
    return text


def _keep(types, row, f):
    pos, op = f["col"], f["op"]
    if op in ("in", "not_in"):
        d = _dim(types, row, pos, f.get("grain"))
        vals = f.get("values", [])
        hit = (d is None and None in vals) or (d is not None and d in [v for v in vals if v is not None])
        return hit if op == "in" else not hit
    if op == "blank":
        return _dim(types, row, pos, f.get("grain")) is None
    if op == "not_blank":
        return _dim(types, row, pos, f.get("grain")) is not None
    if op == "contains":
        d = _dim(types, row, pos, f.get("grain")) or ""
        return f["value"].lower() in d.lower()
    v = _val(types, row, pos)
    if op == "between":
        if v is None:
            return f.get("from") is None and f.get("to") is None
        if f.get("from") is not None and v < _cast(types, pos, f["from"]):
            return False
        if f.get("to") is not None and v > _cast(types, pos, f["to"]):
            return False
        return True
    if v is None:
        return False
    c = _cast(types, pos, f["value"])
    return {"gt": v > c, "gte": v >= c, "lt": v < c, "lte": v <= c, "eq": v == c, "ne": v != c}[op]


def _measure(types, rows, m):
    agg = m["agg"]
    if agg == "count_rows":
        return len(rows)
    pos = m["col"]
    if agg == "count":
        return sum(1 for r in rows if _dim(types, r, pos) is not None)
    if agg == "distinct":
        return len({_dim(types, r, pos) for r in rows} - {None})
    xs = [x for x in (_val(types, r, pos) for r in rows) if x is not None]
    if not xs:
        return None
    if agg == "sum":
        return sum(xs, Decimal(0))
    if agg == "avg":
        return sum(xs, Decimal(0)) / len(xs)
    if agg == "min":
        return min(xs)
    if agg == "max":
        return max(xs)
    raise ValueError(agg)


def _text(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


def run(types: dict[int, str], data: list[list], spec: dict) -> dict:
    """types: {pos: type}; data: list of val arrays (data rows only)."""
    rows = [r for r in data if all(_keep(types, r, f) for f in spec.get("filters") or [])]
    measures = spec["measures"]
    total = {"n": len(rows), "m": [_text(_measure(types, rows, m)) for m in measures]}
    gb = spec.get("group_by")
    if not gb:
        return {"groups": [], "total": total, "group_count": 0}

    groups: dict = {}
    for r in rows:
        groups.setdefault(_dim(types, r, gb["col"], gb.get("grain")), []).append(r)

    def rank_key(item):
        g, rs = item
        m1 = _measure(types, rs, measures[0])
        # m1 desc nulls last, n desc, g asc (byte order) nulls last
        return (m1 is None, -m1 if m1 is not None and not isinstance(m1, dt.date) else 0,
                -(m1.toordinal()) if isinstance(m1, dt.date) else 0,
                -len(rs), g is None, (g or "").encode("utf-8"))

    ordered = sorted(groups.items(), key=rank_key)
    limit = spec.get("limit")
    keep = ordered if limit is None else ordered[:limit]
    rest = [] if limit is None else ordered[limit:]
    out = [{"g": g, "other": False, "n": len(rs),
            "m": [_text(_measure(types, rs, m)) for m in measures]} for g, rs in keep]
    if rest:
        rs = [r for _, grp in rest for r in grp]
        out.append({"g": None, "other": True, "n": len(rs),
                    "m": [_text(_measure(types, rs, m)) for m in measures]})
    return {"groups": out, "total": total, "group_count": len(groups)}


class RefClient:
    """Looks like the Supabase client to dataset_store, but runs on ref_engine.
    Holds datasets in memory. Enough for dashboard tests without Postgres."""

    def __init__(self):
        self.datasets: dict[int, dict] = {}
        self._next = 1

    # dataset_store uses table() for metadata and rpc() for queries
    def table(self, name):
        return _RefTable(self, name)

    def rpc(self, fn, params):
        import types as _t
        ds = self.datasets[params["p_dataset_id"]]
        types = {c["pos"]: c["type"] for c in ds["columns"]}
        data = [r["val"] for r in ds["records"] if r["status"] == "data"]

        def run_it():
            if fn == "ds_query":
                return _t.SimpleNamespace(data=[run(types, data, s) for s in params["p_specs"]])
            if fn == "ds_rows":
                spec = params["p_spec"]
                hit = [r for r in ds["records"] if r["status"] == "data"
                       and all(_keep(types, r["val"], f) for f in spec.get("filters") or [])]
                lim, off = spec.get("limit", 100), spec.get("offset", 0)
                return _t.SimpleNamespace(data={
                    "matched": len(hit),
                    "rows": [{"row": r["row_num"], "cells": r["raw"]} for r in hit[off:off + lim]]})
            if fn == "ds_get_records":
                recs = [[r["row_num"], r["raw"]] for r in ds["records"] if r["row_num"] > params["p_after"]]
                return _t.SimpleNamespace(data=recs[:params["p_limit"]])
            if fn == "ds_put_records":
                by = {r["row_num"]: r for r in ds["records"]}
                for row_num, status, val in params["p_items"]:
                    by[row_num]["status"], by[row_num]["val"] = status, val
                return _t.SimpleNamespace(data=len(params["p_items"]))
            raise NotImplementedError(fn)
        return _t.SimpleNamespace(execute=run_it)


class _RefTable:
    def __init__(self, client, name):
        self.c, self.name = client, name
        self.op, self.payload, self.filters = "select", None, {}

    def select(self, *_a, **_k):
        self.op = "select"; return self

    def insert(self, rows):
        self.op, self.payload = "insert", rows if isinstance(rows, list) else [rows]; return self

    def update(self, values):
        self.op, self.payload = "update", values; return self

    def delete(self):
        self.op = "delete"; return self

    def eq(self, col, val):
        self.filters[col] = val; return self

    def order(self, *_a, **_k):
        return self

    def execute(self):
        import types as _t
        c, data = self.c, []
        if self.name == "datasets":
            if self.op == "insert":
                for row in self.payload:
                    row = {**row, "id": c._next, "columns": [], "records": []}
                    c.datasets[c._next] = row
                    c._next += 1
                    data.append(row)
            elif self.op == "select":
                data = [{k: v for k, v in d.items() if k not in ("columns", "records")}
                        for d in c.datasets.values()
                        if all(d.get(k) == v for k, v in self.filters.items())]
            elif self.op == "update":
                c.datasets[self.filters["id"]].update(self.payload)
            elif self.op == "delete":
                c.datasets.pop(self.filters["id"], None)
        elif self.name == "dataset_columns":
            ds = c.datasets[self.filters["dataset_id"]] if self.filters else None
            if self.op == "insert":
                c.datasets[self.payload[0]["dataset_id"]]["columns"].extend(self.payload)
            elif self.op == "select":
                data = sorted(ds["columns"], key=lambda x: x["pos"])
            elif self.op == "delete":
                ds["columns"] = []
        elif self.name == "dataset_records":
            if self.op == "insert":
                c.datasets[self.payload[0]["dataset_id"]]["records"].extend(self.payload)
        return _t.SimpleNamespace(data=data)
