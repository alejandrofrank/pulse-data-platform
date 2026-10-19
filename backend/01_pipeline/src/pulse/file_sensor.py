"""File discovery for Dagster; the ingestion ledger remains authoritative."""

import hashlib
import json
import os
import uuid
from pathlib import Path

from dagster import DagsterRunStatus, RunRequest, RunsFilter, SensorResult, SkipReason

from pulse.db import connect

ACTIVE = [
    DagsterRunStatus.QUEUED,
    DagsterRunStatus.NOT_STARTED,
    DagsterRunStatus.STARTING,
    DagsterRunStatus.STARTED,
    DagsterRunStatus.CANCELING,
]


def evaluate_files(context):
    drops = Path(os.environ.get("PULSE_DATA_DIR", "data")) / "drops"
    if not drops.is_dir():
        raise FileNotFoundError(f"CSV delivery directory is unavailable: {drops}")
    previous = json.loads(context.cursor)["files"] if context.cursor else {}
    observed, stable = {}, {}
    for path in sorted(drops.glob("*.csv")):
        if not path.is_file() or path.resolve().parent != drops.resolve():
            context.log.warning("Ignoring non-file or external symlink: %s", path.name)
            continue
        try:
            before = path.stat()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            after = path.stat()
        except OSError as exc:
            context.log.warning("Could not inspect %s: %s", path.name, exc)
            continue
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            continue
        signature = [after.st_size, after.st_mtime_ns, digest]
        old = previous.get(path.name)
        unchanged = old is not None and old["signature"] == signature
        # This persisted identity distinguishes separate arrivals of the same bytes while
        # retaining a stable run key across repeated evaluations of one arrival.
        observed[path.name] = {
            "signature": signature,
            "arrival": old["arrival"] if unchanged else str(uuid.uuid4()),
        }
        if unchanged:
            stable[path.name] = digest
    cursor = json.dumps({"version": 1, "files": observed}, sort_keys=True)

    def skip(message):
        return SensorResult(skip_reason=SkipReason(message), cursor=cursor)

    if context.instance.get_runs(
        filters=RunsFilter(job_name="ingest_all", statuses=ACTIVE), limit=1
    ):
        return skip("An ingestion run is queued or active; files will be checked again")
    with connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout='5s'")
        deliveries = {
            r["filename"]: r["sha256"]
            for r in conn.execute("SELECT filename,sha256 FROM raw.deliveries")
        }
        errors = conn.execute(
            "SELECT filename,sha256 FROM ops.file_errors WHERE resolved_at IS NULL"
        ).fetchall()
    rejected = {(r["filename"], r["sha256"]) for r in errors}
    unresolved = {r["filename"] for r in errors}
    selected = {
        name: sha
        for name, sha in stable.items()
        if (name, sha) not in rejected and (deliveries.get(name) != sha or name in unresolved)
    }
    if not selected:
        return skip("No new stable CSVs; already loaded or quarantined content is not resubmitted")
    identity = [(name, sha, observed[name]["arrival"]) for name, sha in sorted(selected.items())]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    return SensorResult(
        run_requests=[
            RunRequest(
                run_key=f"csv-{key}",
                run_config={
                    "ops": {
                        "monthly_metrics": {
                            "config": {
                                "file_names": sorted(selected),
                                "expected_sha256": selected,
                            }
                        }
                    }
                },
                tags={"pulse/trigger": "file_sensor", "pulse/file_count": str(len(selected))},
            )
        ],
        cursor=cursor,
    )
