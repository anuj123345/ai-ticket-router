"""
A tiny stand-in for the Supabase client that talks to a local Postgres, so
the real SQL functions can be tested. Supports only what dataset_store uses.
Connects as service_role, so missing grants fail here too.
"""
import types

import psycopg2
import psycopg2.extras


class _Query:
    def __init__(self, conn, table):
        self.conn, self.table = conn, table
        self.op, self.payload, self.cols = "select", None, "*"
        self.filters, self.orders = [], []

    def select(self, cols="*"):
        self.op, self.cols = "select", cols
        return self

    def insert(self, rows):
        self.op, self.payload = "insert", rows if isinstance(rows, list) else [rows]
        return self

    def update(self, values):
        self.op, self.payload = "update", values
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, val):
        self.filters.append((col, val))
        return self

    def order(self, col):
        self.orders.append(col)
        return self

    def execute(self):
        where = " and ".join(f'"{c}" = %s' for c, _ in self.filters) or "true"
        args = [v for _, v in self.filters]
        with self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if self.op == "select":
                order = (" order by " + ", ".join(self.orders)) if self.orders else ""
                cur.execute(f"select {self.cols} from public.{self.table} where {where}{order}", args)
                data = [dict(r) for r in cur.fetchall()]
            elif self.op == "insert":
                keys = list(self.payload[0].keys())
                collist = ", ".join(f'"{k}"' for k in keys)
                cur.execute(
                    f"insert into public.{self.table} ({collist}) select {collist} "
                    f"from jsonb_populate_recordset(null::public.{self.table}, %s::jsonb) returning *",
                    [psycopg2.extras.Json(self.payload)])
                data = [dict(r) for r in cur.fetchall()]
            elif self.op == "update":
                sets = ", ".join(f'"{k}" = %s' for k in self.payload)
                vals = [psycopg2.extras.Json(v) if isinstance(v, (dict, list)) else v
                        for v in self.payload.values()]
                cur.execute(f"update public.{self.table} set {sets} where {where}", vals + args)
                data = []
            else:
                cur.execute(f"delete from public.{self.table} where {where}", args)
                data = []
        self.conn.commit()
        return types.SimpleNamespace(data=data)


class PgClient:
    def __init__(self, dsn):
        self.conn = psycopg2.connect(dsn)
        with self.conn.cursor() as cur:
            cur.execute("set role service_role")
        self.conn.commit()

    def table(self, name):
        return _Query(self.conn, name)

    def rpc(self, fn, params):
        names = ", ".join(f"{k} => %s" for k in params)
        vals = [psycopg2.extras.Json(v) if isinstance(v, (dict, list)) else v
                for v in params.values()]
        conn = self.conn

        def run():
            try:
                with conn.cursor() as cur:
                    cur.execute(f"select public.{fn}({names})", vals)
                    out = cur.fetchone()[0]
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return types.SimpleNamespace(data=out)
        return types.SimpleNamespace(execute=run)

    def sql(self, statement, args=None):
        with self.conn.cursor() as cur:
            cur.execute(statement, args)
            out = cur.fetchall() if cur.description else None
        self.conn.commit()
        return out
