import hashlib
import os
from importlib.resources import files

import psycopg
from psycopg.rows import dict_row


def connect(url=None):
    return psycopg.connect(url or os.environ["DATABASE_URL"], row_factory=dict_row)


def migrate(url=None, *, include_lab=False):
    with connect(url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(7281901)")
        conn.execute("""CREATE TABLE IF NOT EXISTS public.schema_migrations (
            name text PRIMARY KEY, sha256 text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now())""")
        applied = {
            r["name"]: r["sha256"]
            for r in conn.execute("SELECT name,sha256 FROM public.schema_migrations")
        }
        paths = [(p.name, p) for p in files("pulse").joinpath("migrations").iterdir()]
        if include_lab:
            paths.extend(
                (f"lab/{p.name}", p) for p in files("pulse").joinpath("lab_migrations").iterdir()
            )
        for name, path in sorted(paths):
            if not path.name.endswith(".sql"):
                continue
            body = path.read_bytes()
            checksum = hashlib.sha256(body).hexdigest()
            if name in applied:
                if applied[name] != checksum:
                    raise RuntimeError(f"Applied migration changed: {name}")
                continue
            conn.execute(body.decode("utf-8"))
            conn.execute(
                "INSERT INTO public.schema_migrations(name,sha256) VALUES (%s,%s)",
                (name, checksum),
            )
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname='pulse_reader'").fetchone():
            conn.execute("GRANT SELECT ON public.schema_migrations TO pulse_reader")
            conn.execute("GRANT USAGE ON SCHEMA core,raw,ops,mart TO pulse_reader")
            conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA core,raw,ops,mart TO pulse_reader")
