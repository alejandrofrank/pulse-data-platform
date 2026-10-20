"""Durable, one-file-at-a-time playback of the real ingestion pipeline in a lab DB."""

import hashlib
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from pulse.db import connect, migrate
from pulse.ingest import LOCK as INGEST_LOCK
from pulse.ingest import ingest, jsonb, load_master
from pulse.parsing import V1, V2, FileContractError, delivery_key, read_csv

CONTROL_LOCK = 7281910
WORKER_LOCK = 7281911
ACTIVE = ("queued", "running", "paused")


class LabConflict(ValueError):
    pass


def validate_target(url):
    name = urlparse(url).path.lstrip("/")
    if not name.endswith(("_lab", "_test")):
        raise ValueError("Lab database must end in _lab or _test; main databases are forbidden")


def snapshot(conn):
    """Caller supplies a repeatable-read transaction; operational run IDs are excluded."""
    rows = conn.execute("""SELECT asesoria_id,mes,
        (to_jsonb(f)-'published_run_id'-'source_record_id')::text AS document
        FROM mart.firm_month f ORDER BY asesoria_id,mes""").fetchall()
    documents = {f"{r['asesoria_id']}/{r['mes']}": r["document"] for r in rows}
    digest = hashlib.sha256()
    for key, document in documents.items():
        digest.update(f"{key}\t{document}\n".encode())
    counts = conn.execute("""SELECT count(*) AS raw_rows,
        count(*) FILTER (WHERE status='accepted') AS accepted_rows,
        count(*) FILTER (WHERE status='rejected') AS rejected_rows,
        count(*) FILTER (WHERE status='duplicate') AS duplicate_rows FROM raw.records""").fetchone()
    counts.update(
        conn.execute("""SELECT
        (SELECT count(*) FROM raw.deliveries) AS files,
        (SELECT count(*) FROM ops.issues) AS issues,
        (SELECT count(DISTINCT record_id) FROM ops.issues WHERE severity='warning') AS warning_rows,
        (SELECT count(*) FROM mart.coverage WHERE status='missing') AS gaps,
        (SELECT count(*) FROM ops.file_errors WHERE resolved_at IS NULL) AS file_errors
        """).fetchone()
    )
    counts["published_rows"] = len(rows)
    invalid = conn.execute("""SELECT count(*) AS n FROM mart.firm_month f
        JOIN raw.records r ON r.id=f.source_record_id WHERE r.status<>'accepted'""").fetchone()["n"]
    wrong = conn.execute("""WITH expected AS (
        SELECT DISTINCT ON (r.normalized->>'asesoria_id',r.normalized->>'mes')
        r.id,(r.normalized->>'asesoria_id')::integer AS firm,
        (r.normalized->>'mes')::date AS month
        FROM raw.records r JOIN raw.deliveries d ON d.id=r.delivery_id
        WHERE r.status='accepted'
        ORDER BY r.normalized->>'asesoria_id',r.normalized->>'mes',
                 d.delivery_month DESC,d.revision DESC,r.line_number)
        SELECT count(*) AS n FROM expected e FULL JOIN mart.firm_month f
        ON f.asesoria_id=e.firm AND f.mes=e.month WHERE e.id IS DISTINCT FROM f.source_record_id
        """).fetchone()["n"]
    duplicates = conn.execute("""SELECT count(*) AS n FROM (
        SELECT asesoria_id,mes FROM mart.firm_month GROUP BY asesoria_id,mes HAVING count(*)>1
        ) q""").fetchone()["n"]
    return {
        "counts": counts,
        "business_hash": digest.hexdigest(),
        "checks": [
            {
                "name": "unique_keys",
                "label": "Una fila por asesoría y mes",
                "passed": duplicates == 0,
            },
            {"name": "valid_sources", "label": "Solo fuentes aceptadas", "passed": invalid == 0},
            {
                "name": "latest_delivery",
                "label": "Última entrega válida gana",
                "passed": wrong == 0,
            },
        ],
        "documents": documents,
    }


def public_snapshot(value):
    return {k: v for k, v in value.items() if k != "documents"}


def evidence(before, after, result):
    left, right = before["documents"], after["documents"]
    inserted = right.keys() - left.keys()
    removed = left.keys() - right.keys()
    updated = [k for k in left.keys() & right.keys() if left[k] != right[k]]
    replay = result["skipped"] == 1 and not result["failed"]
    unchanged = before["business_hash"] == after["business_hash"]
    unchanged_archive = all(
        before["counts"][k] == after["counts"][k]
        for k in ("files", "raw_rows", "accepted_rows", "rejected_rows", "duplicate_rows", "issues")
    )
    return {
        "run": result,
        "before": public_snapshot(before),
        "after": public_snapshot(after),
        "inserted": len(inserted),
        "updated": len(updated),
        "removed": len(removed),
        "changed_keys": sorted([*inserted, *updated, *removed])[:20],
        "unchanged": len(left.keys() & right.keys()) - len(updated),
        "replay": replay,
        # A retry after a failed publication may legitimately repair reporting on a skipped file.
        "idempotent": unchanged and unchanged_archive if replay else None,
        "recovery": replay and not unchanged and not all(c["passed"] for c in before["checks"]),
    }


class Playback:
    def __init__(self, url, root):
        validate_target(url)
        self.url = url
        self.root = Path(root)
        self.stop_event = threading.Event()
        self.thread = None
        self.worker_error = None

    def start(self):
        conn = connect(self.url)
        conn.autocommit = True
        try:
            if not conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (WORKER_LOCK,)).fetchone()[
                "ok"
            ]:
                raise LabConflict("A lab worker is already running")
            migrate(self.url, include_lab=True)
            with conn.transaction():
                load_master(conn, self.root)
                conn.execute("""UPDATE lab.jobs SET status='paused',pause_requested=false,
                    message='Worker restarted. Resume to retry the unfinished file safely.',
                    updated_at=now() WHERE status IN ('queued','running')""")
                conn.execute("UPDATE lab.steps SET status='pending' WHERE status='running'")
            self.thread = threading.Thread(target=self._loop, args=(conn,), daemon=True)
            self.thread.start()
        except Exception:
            conn.close()
            raise

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=30)

    def _loop(self, conn):
        try:
            while not self.stop_event.is_set():
                with conn.transaction():
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (CONTROL_LOCK,))
                    job = conn.execute("""SELECT * FROM lab.jobs WHERE status='queued'
                        ORDER BY sequence LIMIT 1 FOR UPDATE""").fetchone()
                    if job:
                        conn.execute(
                            "UPDATE lab.jobs SET status='running',updated_at=now() WHERE id=%s",
                            (job["id"],),
                        )
                if job:
                    self._play(conn, job["id"])
                else:
                    self.stop_event.wait(0.2)
        except Exception as exc:
            self.worker_error = str(exc)
        finally:
            conn.close()

    def _read_snapshot(self):
        with connect(self.url) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return snapshot(conn)

    def _play(self, conn, job_id):
        while True:
            job = conn.execute("SELECT * FROM lab.jobs WHERE id=%s", (job_id,)).fetchone()
            if job["cancel_requested"]:
                self._finish(
                    conn, job_id, "cancelled", "Playback cancelled; committed files are retained."
                )
                return
            if job["pause_requested"] or self.stop_event.is_set():
                self._finish(
                    conn,
                    job_id,
                    "paused",
                    "Paused between files; completed deliveries are retained.",
                )
                return
            if job["cursor"] >= len(job["filenames"]):
                self._finish(conn, job_id, "success", "All selected files processed.")
                return
            pos = job["cursor"]
            conn.execute(
                """UPDATE lab.steps SET status='running',started_at=now(),
                finished_at=NULL,message=NULL WHERE job_id=%s AND position=%s""",
                (job_id, pos),
            )
            try:
                before = self._read_snapshot()
                result = ingest(
                    self.root, file_names=[job["filenames"][pos]], database_url=self.url
                )
                after = self._read_snapshot()
                proof = evidence(before, after, result)
                good = result["status"] == "success" and all(c["passed"] for c in after["checks"])
                if proof["replay"] and not proof["idempotent"] and not proof["recovery"]:
                    good = False
                status = ("skipped" if result["skipped"] else "loaded") if good else "failed"
                with conn.transaction():
                    conn.execute(
                        """UPDATE lab.steps SET status=%s,finished_at=now(),evidence=%s
                        WHERE job_id=%s AND position=%s""",
                        (status, jsonb(proof), job_id, pos),
                    )
                    if good:
                        conn.execute(
                            "UPDATE lab.jobs SET cursor=cursor+1,updated_at=now() WHERE id=%s",
                            (job_id,),
                        )
                if not good:
                    self._finish(
                        conn,
                        job_id,
                        "failed",
                        "Quarantine or integrity check failed. Inspect evidence before retrying.",
                    )
                    return
            except Exception as exc:
                conn.execute(
                    """UPDATE lab.steps SET status='failed',finished_at=now(),message=%s
                    WHERE job_id=%s AND position=%s""",
                    (str(exc), job_id, pos),
                )
                self._finish(conn, job_id, "failed", str(exc))
                return
            deadline = time.monotonic() + job["interval_ms"] / 1000
            while time.monotonic() < deadline and not self.stop_event.wait(0.1):
                flags = conn.execute(
                    "SELECT pause_requested,cancel_requested FROM lab.jobs WHERE id=%s", (job_id,)
                ).fetchone()
                if flags["pause_requested"] or flags["cancel_requested"]:
                    break

    @staticmethod
    def _finish(conn, job_id, status, message):
        conn.execute(
            """UPDATE lab.jobs SET status=%s,message=%s,updated_at=now(),
            pause_requested=false,cancel_requested=false WHERE id=%s""",
            (status, message, job_id),
        )

    def _validate_files(self, names):
        if not names or len(names) != len(set(names)):
            raise ValueError("Select at least one distinct file")
        for name in names:
            path = self.root / "drops" / name
            if (
                "/" in name
                or "\\" in name
                or not name.endswith(".csv")
                or path.resolve().parent != (self.root / "drops").resolve()
                or not path.is_file()
            ):
                raise ValueError("Select an existing CSV from the lab catalog")

    def enqueue(self, job_id, names, interval_ms):
        with connect(self.url) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (CONTROL_LOCK,))
            existing = conn.execute("SELECT * FROM lab.jobs WHERE id=%s", (job_id,)).fetchone()
            if existing:
                if existing["filenames"] != names or existing["interval_ms"] != interval_ms:
                    raise LabConflict("Request ID already belongs to a different selection")
                return existing
            if self.worker_error or not self.thread or not self.thread.is_alive():
                raise LabConflict(
                    "Playback worker is unavailable; restart the lab before submitting"
                )
            self._validate_files(names)
            if conn.execute(
                "SELECT 1 FROM lab.jobs WHERE status=ANY(%s)", (list(ACTIVE),)
            ).fetchone():
                raise LabConflict(
                    "Pause/resume or cancel the current playback before starting another"
                )
            row = conn.execute(
                """INSERT INTO lab.jobs(id,filenames,interval_ms,status)
                VALUES (%s,%s,%s,'queued') RETURNING *""",
                (job_id, jsonb(names), interval_ms),
            ).fetchone()
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO lab.steps(job_id,position,filename) VALUES (%s,%s,%s)",
                    [(job_id, i, name) for i, name in enumerate(names)],
                )
            return row

    def control(self, job_id, action):
        with connect(self.url) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (CONTROL_LOCK,))
            job = conn.execute(
                "SELECT * FROM lab.jobs WHERE id=%s FOR UPDATE", (job_id,)
            ).fetchone()
            if not job:
                raise KeyError("Playback not found")
            if action == "resume":
                if job["status"] in ("queued", "running"):
                    return job
                if job["status"] not in ("paused", "failed"):
                    raise LabConflict("Only paused or failed playback can resume")
                if conn.execute(
                    "SELECT 1 FROM lab.jobs WHERE id<>%s AND status=ANY(%s)", (job_id, list(ACTIVE))
                ).fetchone():
                    raise LabConflict("Another playback is active")
                conn.execute(
                    """UPDATE lab.jobs SET status='queued',pause_requested=false,
                    cancel_requested=false,message=NULL,updated_at=now() WHERE id=%s""",
                    (job_id,),
                )
            elif action == "pause":
                if job["status"] == "queued":
                    self._finish(conn, job_id, "paused", "Paused before the next file.")
                elif job["status"] == "running":
                    conn.execute("UPDATE lab.jobs SET pause_requested=true WHERE id=%s", (job_id,))
                elif job["status"] != "paused":
                    raise LabConflict("Playback is not running")
            elif action == "cancel":
                if job["status"] == "running":
                    conn.execute("UPDATE lab.jobs SET cancel_requested=true WHERE id=%s", (job_id,))
                elif job["status"] in ("queued", "paused", "failed"):
                    self._finish(
                        conn, job_id, "cancelled", "Cancelled; committed files are retained."
                    )
            return conn.execute("SELECT * FROM lab.jobs WHERE id=%s", (job_id,)).fetchone()

    def reset(self):
        with connect(self.url) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (CONTROL_LOCK,))
            if conn.execute(
                "SELECT 1 FROM lab.jobs WHERE status IN ('queued','running')"
            ).fetchone():
                raise LabConflict("Pause playback and wait for the current file before resetting")
            if not conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s) AS locked", (INGEST_LOCK,)
            ).fetchone()["locked"]:
                raise LabConflict("An ingestion is running; wait before resetting the lab")
            # Explicit table list, fixed lab connection, no database/schema drop or source deletion.
            conn.execute("""TRUNCATE lab.steps,lab.jobs,mart.firm_month,ops.issues,
                ops.file_errors,ops.publication_deliveries,ops.publications,
                raw.records,raw.deliveries,ops.runs RESTART IDENTITY""")

    def get_job(self, job_id):
        with connect(self.url) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            job = conn.execute("SELECT * FROM lab.jobs WHERE id=%s", (job_id,)).fetchone()
            if not job:
                raise KeyError("Playback not found")
            job["steps"] = conn.execute(
                "SELECT * FROM lab.steps WHERE job_id=%s ORDER BY position", (job_id,)
            ).fetchall()
            return job

    def state(self):
        with connect(self.url) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            current = public_snapshot(snapshot(conn))
            deliveries = {
                r["filename"]: r
                for r in conn.execute("""SELECT d.filename,d.sha256,
                count(r.id) AS rows,count(r.id) FILTER (WHERE r.status='rejected') AS rejected
                FROM raw.deliveries d LEFT JOIN raw.records r ON r.delivery_id=d.id
                GROUP BY d.id""")
            }
            jobs = conn.execute("SELECT * FROM lab.jobs ORDER BY sequence DESC LIMIT 20").fetchall()
            job = next((j for j in jobs if j["status"] in ACTIVE), jobs[0] if jobs else None)
            if job:
                job["steps"] = conn.execute(
                    "SELECT * FROM lab.steps WHERE job_id=%s ORDER BY position", (job["id"],)
                ).fetchall()
            months = conn.execute(
                "SELECT mes,revenue_eur,clientes_activos,reporting_firms,"
                "expected_firms,missing_firms,coverage_pct "
                "FROM mart.network_month ORDER BY mes"
            ).fetchall()
            problems = conn.execute("""SELECT d.filename,r.line_number,i.code,i.severity,i.message
                FROM ops.issues i JOIN raw.records r ON r.id=i.record_id
                JOIN raw.deliveries d ON d.id=r.delivery_id
                ORDER BY i.id DESC LIMIT 12""").fetchall()
            file_problems = conn.execute("""SELECT filename,reason,last_seen_at
                FROM ops.file_errors WHERE resolved_at IS NULL
                ORDER BY last_seen_at DESC LIMIT 12""").fetchall()
        catalog = []
        for path in sorted((self.root / "drops").glob("*.csv")):
            if path.resolve().parent != (self.root / "drops").resolve():
                continue
            content = path.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            previous = deliveries.get(path.name)
            item = {
                "filename": path.name,
                "sha256": digest,
                "bytes": len(content),
                "status": "pending"
                if not previous
                else "loaded"
                if previous["sha256"] == digest
                else "changed",
                "month": None,
                "revision": None,
                "rows": None,
                "format": None,
                "rejected": previous["rejected"] if previous else None,
            }
            try:
                month, revision = delivery_key(path.name)
                fields, rows, _ = read_csv(content)
                item.update(
                    month=month,
                    revision=revision,
                    rows=len(rows),
                    format="v1" if fields == V1 else "v2" if fields == V2 else "unknown",
                )
            except FileContractError as exc:
                item["error"] = str(exc)
            catalog.append(item)
        return {
            **current,
            "catalog": catalog,
            "job": job,
            "jobs": jobs,
            "months": months,
            "issues": problems,
            "file_errors": file_problems,
            "worker_error": self.worker_error,
            "worker_alive": bool(self.thread and self.thread.is_alive()),
        }
