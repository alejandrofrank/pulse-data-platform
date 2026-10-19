"""Durable raw ingestion followed by an atomic, deterministic reporting projection."""

import hashlib
import json
import os
import uuid
from collections import Counter
from pathlib import Path

from psycopg.types.json import Jsonb

from pulse.db import connect
from pulse.parsing import (
    COUNTS,
    MASTER,
    METRICS,
    POLICY_VERSION,
    FileContractError,
    delivery_key,
    parse_drop,
    parse_master,
)

LOCK = 7281902


def jsonb(value):
    return Jsonb(value, dumps=lambda v: json.dumps(v, default=str))


def load_master(conn, root):
    content = (root / "asesorias.csv").read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    firms = parse_master(content)
    existing = conn.execute("SELECT sha256 FROM raw.master").fetchone()
    if existing and existing["sha256"] != checksum:
        raise FileContractError(
            "Master changed: review and migrate it explicitly; history is immutable"
        )
    if not existing:
        conn.execute("INSERT INTO raw.master(sha256,content) VALUES (%s,%s)", (checksum, content))
        with conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO core.firms ({','.join(MASTER)}) VALUES ({','.join(['%s'] * 8)})",
                [tuple(firm[k] for k in MASTER) for firm in firms.values()],
            )
    return firms


def file_error(conn, path, content, checksum, reason, run_id):
    conn.execute(
        """INSERT INTO ops.file_errors(filename,sha256,content,reason,run_id)
        VALUES (%s,%s,%s,%s,%s) ON CONFLICT(filename,sha256,reason) DO UPDATE
        SET last_seen_at=now(),run_id=excluded.run_id,resolved_at=NULL""",
        (path.name, checksum, content, reason, run_id),
    )


def load_file(conn, path, firms, run_id, expected_sha256=None):
    content = path.read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    if expected_sha256 is not None and checksum != expected_sha256:
        raise FileContractError(
            f"{path.name} changed after sensor inspection; wait for a new stable delivery"
        )
    existing = conn.execute(
        "SELECT sha256,policy_version FROM raw.deliveries WHERE filename=%s", (path.name,)
    ).fetchone()
    if existing:
        if existing["sha256"] != checksum:
            file_error(
                conn,
                path,
                content,
                checksum,
                "Existing filename changed; submit an explicit __NNN revision",
                run_id,
            )
            return "failed"
        if existing["policy_version"] != POLICY_VERSION:
            raise RuntimeError("Policy version changed: rebuild in a new database before promotion")
        conn.execute(
            "UPDATE ops.file_errors SET resolved_at=now() WHERE filename=%s "
            "AND resolved_at IS NULL",
            (path.name,),
        )
        return "skipped"
    try:
        month, revision = delivery_key(path.name)
        collision = conn.execute(
            "SELECT 1 FROM raw.deliveries WHERE delivery_month=%s AND revision=%s",
            (month, revision),
        ).fetchone()
        if collision:
            raise FileContractError(
                "Another filename already occupies this delivery month/revision"
            )
        version, rows = parse_drop(content, path.name, firms)
        if not rows:
            raise FileContractError("Empty delivery")
    except FileContractError as exc:
        file_error(conn, path, content, checksum, str(exc), run_id)
        return "failed"
    delivery_id = conn.execute(
        """INSERT INTO raw.deliveries
        (filename,delivery_month,revision,sha256,content,schema_version,policy_version,run_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (path.name, month, revision, checksum, content, version, POLICY_VERSION, run_id),
    ).fetchone()["id"]
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO raw.records
            (delivery_id,line_number,payload,normalized,status) VALUES (%s,%s,%s,%s,%s)""",
            [(delivery_id, r.line, jsonb(r.raw), jsonb(r.values), r.status) for r in rows],
        )
    ids = {
        r["line_number"]: r["id"]
        for r in conn.execute(
            "SELECT id,line_number FROM raw.records WHERE delivery_id=%s", (delivery_id,)
        )
    }
    with conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO ops.issues(record_id,severity,code,field,message)
            VALUES (%s,%s,%s,%s,%s)""",
            [(ids[r.line], i.severity, i.code, i.field, i.message) for r in rows for i in r.issues],
        )
    conn.execute("UPDATE ops.file_errors SET resolved_at=now() WHERE filename=%s", (path.name,))
    return "loaded"


def publish(conn, run_id):
    # 18k rows: rebuild from immutable normalized history for order-independent correctness.
    # DELETE is MVCC-safe: readers see the old complete version until the transaction commits.
    conn.execute("DELETE FROM mart.firm_month")
    types = {name: "integer" if name in COUNTS else "numeric(18,2)" for name in METRICS}
    projection = ",".join(f"n.{name}" for name in METRICS)
    declarations = ",".join(f"{name} {kind}" for name, kind in types.items())
    conn.execute(
        f"""WITH winners AS (
        SELECT DISTINCT ON (r.normalized->>'asesoria_id',r.normalized->>'mes')
            r.*,d.delivery_month,d.revision
        FROM raw.records r JOIN raw.deliveries d ON d.id=r.delivery_id
        WHERE r.status='accepted'
        ORDER BY r.normalized->>'asesoria_id',r.normalized->>'mes',
                 d.delivery_month DESC,d.revision DESC,r.line_number
    ) INSERT INTO mart.firm_month
      (asesoria_id,mes,{",".join(METRICS)},source_record_id,
       has_rejected_newer_delivery,published_run_id)
    SELECT n.asesoria_id,n.mes,{projection},w.id,
      EXISTS (
        SELECT 1 FROM raw.records bad JOIN raw.deliveries bd ON bd.id=bad.delivery_id
        JOIN raw.record_identity bi ON bi.record_id=bad.id
        WHERE bad.status='rejected'
          AND bi.asesoria_id=w.normalized->>'asesoria_id'
          AND bi.mes=w.normalized->>'mes'
          AND (bd.delivery_month,bd.revision)>(w.delivery_month,w.revision)
      ), %s
    FROM winners w CROSS JOIN LATERAL jsonb_to_record(w.normalized)
         AS n(asesoria_id integer,mes date,{declarations})""",
        (run_id,),
    )
    conn.execute(
        """INSERT INTO ops.publications(id,policy_version,record_count)
        SELECT %s,%s,count(*) FROM mart.firm_month""",
        (run_id, POLICY_VERSION),
    )
    conn.execute(
        """INSERT INTO ops.publication_deliveries(publication_id,delivery_id)
        SELECT %s,id FROM raw.deliveries""",
        (run_id,),
    )


def ingest(
    data_dir=None, dagster_run_id=None, *, file_names=None, database_url=None, expected_sha256=None
):
    root = Path(data_dir or os.environ.get("PULSE_DATA_DIR", "data"))
    if file_names is None:
        paths = sorted((root / "drops").glob("*.csv"))
    else:
        if not file_names or len(set(file_names)) != len(file_names):
            raise ValueError("Select at least one distinct CSV filename")
        paths = []
        for name in file_names:
            if not isinstance(name, str):
                raise ValueError("Selected CSV names must be strings")
            path = root / "drops" / name
            if (
                "/" in name
                or "\\" in name
                or not name.endswith(".csv")
                or path.resolve().parent != (root / "drops").resolve()
                or not path.is_file()
            ):
                raise ValueError("Selected CSV must exist directly inside drops/")
            paths.append(path)
    if expected_sha256 is not None and (
        file_names is None or set(expected_sha256) != set(file_names)
    ):
        raise ValueError("Expected checksums must match the explicitly selected filenames")
    run_id = uuid.uuid4()
    counts = Counter(loaded=0, skipped=0, failed=0)
    with connect(database_url) as conn:
        conn.autocommit = True
        locked = conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (LOCK,)).fetchone()
        if not locked["locked"]:
            raise RuntimeError("Another ingestion is running; retry after it finishes")
        conn.execute("""UPDATE ops.runs SET status='abandoned',finished_at=now(),
            message='Previous worker stopped before completion' WHERE status='running'""")
        conn.execute(
            """INSERT INTO ops.runs(id,dagster_run_id,status,policy_version)
            VALUES (%s,%s,'running',%s)""",
            (run_id, dagster_run_id, POLICY_VERSION),
        )
        try:
            with conn.transaction():
                firms = load_master(conn, root)
            if not paths:
                raise FileContractError("No CSV deliveries found in drops/")
            for path in paths:
                with conn.transaction():
                    counts[
                        load_file(
                            conn,
                            path,
                            firms,
                            run_id,
                            expected_sha256.get(path.name) if expected_sha256 is not None else None,
                        )
                    ] += 1
            with conn.transaction():
                publish(conn, run_id)
                status = "partial" if counts["failed"] else "success"
                conn.execute(
                    """UPDATE ops.runs SET status=%s,finished_at=now(),
                    files_loaded=%s,files_skipped=%s,files_failed=%s WHERE id=%s""",
                    (status, counts["loaded"], counts["skipped"], counts["failed"], run_id),
                )
            return {"run_id": str(run_id), "status": status, **counts}
        except Exception as exc:
            conn.execute(
                """UPDATE ops.runs SET status='failed',finished_at=now(),
                files_loaded=%s,files_skipped=%s,files_failed=%s,message=%s WHERE id=%s""",
                (counts["loaded"], counts["skipped"], counts["failed"], str(exc), run_id),
            )
            raise
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))
