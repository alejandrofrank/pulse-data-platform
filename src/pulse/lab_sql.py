"""User-written SELECT queries, parsed by PostgreSQL and executed as the reader role."""

import os
import threading
import time
from contextlib import contextmanager

import psycopg
from psycopg import sql

from pulse.db import connect

QUERY_SECONDS = 5
MAX_BYTES = 2_000_000
SLOTS = threading.BoundedSemaphore(2)
DECLARE = "DECLARE pulse_query NO SCROLL CURSOR FOR "


class QueryUnavailable(ValueError):
    pass


@contextmanager
def reader():
    url = os.environ["DATABASE_URL"]
    with connect(url) as conn:
        # Never expose the owner connection, including under an accidental misconfiguration.
        if conn.info.user != "pulse_reader":
            raise QueryUnavailable("SQL requires the pulse_reader connection")
        if not conn.info.dbname.endswith(("_lab", "_test")):
            raise QueryUnavailable("SQL must connect to a lab database")
        try:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            conn.execute("SET LOCAL statement_timeout = '5s'")
            conn.execute("SET LOCAL lock_timeout = '1s'")
            conn.execute("SET LOCAL search_path = pg_catalog")
            # Independent deadline also covers repeated FETCH calls and set_config() expressions.
            deadline = time.monotonic() + QUERY_SECONDS

            def cancel():
                try:
                    conn.cancel_safe(timeout=1)
                except psycopg.Error:
                    pass

            timer = threading.Timer(QUERY_SECONDS, cancel)
            timer.daemon = True
            timer.start()
            try:
                yield conn, deadline
            finally:
                timer.cancel()
                timer.join()
        finally:
            conn.rollback()


def schema():
    with reader() as (conn, _):
        context = conn.execute(
            "SELECT current_database() AS database,current_user AS role"
        ).fetchone()
        columns = conn.execute("""SELECT n.nspname AS schema,c.relname AS name,
            CASE c.relkind WHEN 'v' THEN 'view' ELSE 'table' END AS kind,
            a.attname AS column,format_type(a.atttypid,a.atttypmod) AS type,
            NOT a.attnotnull AS nullable,
            EXISTS(SELECT 1 FROM pg_index i WHERE i.indrelid=c.oid AND i.indisprimary
                   AND a.attnum=ANY(i.indkey)) AS primary_key
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
            WHERE n.nspname IN ('raw','core','mart','ops','lab') AND c.relkind IN ('r','v')
            AND has_table_privilege(c.oid,'SELECT')
            ORDER BY n.nspname,c.relname,a.attnum""").fetchall()
        tables = {}
        for column in columns:
            key = (column["schema"], column["name"])
            table = tables.setdefault(key, {k: column[k] for k in ("schema", "name", "kind")})
            table.setdefault("columns", []).append(
                {k: column[k] for k in ("column", "type", "nullable", "primary_key")}
            )
        return {**context, "tables": list(tables.values()), "timeout_seconds": QUERY_SECONDS}


def query(text, limit=500, explain=False):
    if not SLOTS.acquire(blocking=False):
        raise QueryUnavailable("Two SQL queries are already running; try again shortly")
    start = time.monotonic()
    try:
        with reader() as (conn, deadline):
            context = conn.execute("""SELECT current_database() AS database,current_user AS role,
                transaction_timestamp() AS snapshot_at""").fetchone()
            # PostgreSQL's DECLARE grammar accepts SELECT/WITH/VALUES/TABLE, not commands.
            # Extended-protocol preparation rejects multiple statements, including injected
            # COMMIT/SET commands. No regex parser, interpolated filter, or owner connection.
            conn.execute(sql.SQL(DECLARE) + sql.SQL(text), prepare=True)
            if explain:
                cur = conn.execute(sql.SQL("EXPLAIN (FORMAT TEXT) ") + sql.SQL(text), prepare=True)
                columns = [{"name": c.name, "type_oid": c.type_code} for c in cur.description]
                rows = [
                    [cur.pgresult.get_value(i, 0).decode()] for i in range(cur.pgresult.ntuples)
                ]
                truncated = False
                reason = None
            else:
                rows, columns, size = [], [], 0
                truncated, reason = False, None
                while len(rows) <= limit:
                    if time.monotonic() >= deadline:
                        raise psycopg.errors.QueryCanceled("SQL exceeded the five-second limit")
                    count = min(50, limit + 1 - len(rows))
                    cur = conn.execute(
                        sql.SQL("FETCH FORWARD {} FROM pulse_query").format(sql.Literal(count))
                    )
                    columns = [{"name": c.name, "type_oid": c.type_code} for c in cur.description]
                    result = cur.pgresult
                    for i in range(result.ntuples):
                        if len(rows) == limit:
                            truncated, reason = True, "row_limit"
                            break
                        values = [result.get_value(i, j) for j in range(result.nfields)]
                        row_size = sum(len(v) if v is not None else 4 for v in values)
                        if size + row_size > MAX_BYTES:
                            truncated, reason = True, "byte_limit"
                            break
                        # PostgreSQL text values preserve numeric/bigint precision, JSON, arrays,
                        # duplicate column names and NULL without JavaScript number coercion.
                        rows.append([v.decode("utf-8") if v is not None else None for v in values])
                        size += row_size
                    if truncated or result.ntuples < count:
                        break
            types = conn.execute(
                "SELECT oid,format_type(oid,NULL) AS name FROM pg_type WHERE oid=ANY(%s)",
                ([c["type_oid"] for c in columns],),
            ).fetchall()
            names = {r["oid"]: r["name"] for r in types}
            for column in columns:
                column["type"] = names.get(column["type_oid"], str(column["type_oid"]))
            return {
                **context,
                "columns": columns,
                "rows": rows,
                "row_count": len(rows),
                "truncated": truncated,
                "truncation_reason": reason,
                "limit": limit,
                "elapsed_ms": round((time.monotonic() - start) * 1000),
                "explain": explain,
            }
    finally:
        SLOTS.release()


def error_detail(exc):
    if isinstance(exc, psycopg.Error):
        return {
            "message": exc.diag.message_primary or str(exc),
            "sqlstate": exc.sqlstate,
            "hint": exc.diag.message_hint,
        }
    return {"message": str(exc)}
