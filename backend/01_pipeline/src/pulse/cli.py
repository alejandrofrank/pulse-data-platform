import argparse
import os
import sys
from contextlib import contextmanager
from threading import Timer

from pulse.db import connect, migrate

# A manual/bootstrapping execute_in_process worker has no launcher termination handle.
# Kill this process at 15 minutes, before Dagster's 16-minute monitoring deadline.
# Keep this budget below start_timeout_seconds and max_runtime_seconds in dagster.yaml.
# SIGKILL/container loss can prevent cleanup; monitoring then releases the queue slot.
CLI_EXECUTION_BUDGET_SECONDS = 15 * 60


@contextmanager
def execution_deadline(seconds=CLI_EXECUTION_BUDGET_SECONDS):
    """Enforce the CLI budget even when Python exception/transaction cleanup stalls.

    Exit 124 closes the worker's connections, rolling back its open transaction and
    releasing its writer lock. Earlier raw commits survive. Dagster monitoring marks
    the unfinished run failed after its deadline; a subsequent full ingest republishes.
    This is an execution limit, not evidence that an old run's worker has died.
    """
    timer = Timer(seconds, lambda: os._exit(124))
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()


def diagnostic_failure_kind(result):
    """Recognize explicit quarantine or the empty-publication blocking check only."""
    from pulse.definitions import EMPTY_PUBLICATION_FAILURE, QUARANTINE_FAILURE

    # A known step failure must not hide an additional run/engine infrastructure error.
    if result.get_run_failure_event().event_specific_data.error is not None:
        return None
    failures = result.get_step_failure_events()
    if len(failures) != 1:
        return None
    event = failures[0]
    failure = event.event_specific_data
    if event.step_key == "monthly_metrics" and failure.user_failure_data is not None:
        marker = failure.user_failure_data.metadata.get(QUARANTINE_FAILURE)
        if marker is not None and marker.value is True:
            return "quarantine"
    if (
        event.step_key == "monthly_metrics_publication_integrity"
        and failure.error is not None
        and failure.error.cls_name == "DagsterAssetCheckFailedError"
    ):
        for check in result.get_asset_check_evaluations():
            marker = check.metadata.get(EMPTY_PUBLICATION_FAILURE)
            if (
                check.asset_key.path == ["monthly_metrics"]
                and check.check_name == "publication_integrity"
                and not check.passed
                and marker is not None
                and marker.value is True
                and check.metadata["curated_rows"].value == 0
                and check.metadata["invalid_sources"].value == 0
            ):
                return "empty"
    return None


def diagnostic_publication_ready(kind, dagster_run_id):
    """Check this run committed a valid publication with accounted quality failures.

    Database failures deliberately propagate. A prior good publication must never
    conceal migration, infrastructure, integrity-check or unexpected asset failures.
    """
    with connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout='5s'")
        return conn.execute(
            """SELECT EXISTS (
                SELECT 1 FROM ops.runs r JOIN ops.publications p ON p.id=r.id
                WHERE r.dagster_run_id=%s
                  AND p.id=(SELECT id FROM ops.publications ORDER BY published_at DESC LIMIT 1)
                  AND ((%s='quarantine' AND r.status='partial' AND r.files_failed>0)
                    OR (%s='empty' AND r.status='success' AND r.files_failed=0
                        AND p.record_count=0))
                  AND p.record_count=(SELECT count(*) FROM mart.firm_month)
                  AND (SELECT count(*) FROM ops.publication_deliveries WHERE publication_id=p.id)
                      =(SELECT count(*) FROM raw.deliveries)
                  AND NOT EXISTS (
                    SELECT 1 FROM mart.firm_month f
                    LEFT JOIN raw.records source ON source.id=f.source_record_id
                    WHERE f.published_run_id<>p.id OR source.status IS DISTINCT FROM 'accepted'
                  )
                  AND (p.record_count>0 OR (
                    NOT EXISTS (SELECT 1 FROM raw.records WHERE status='accepted')
                    AND (r.files_failed>0 OR EXISTS (
                      SELECT 1 FROM raw.records WHERE status='rejected'
                    ))
                  ))
            ) AS ready""",
            (dagster_run_id, kind, kind),
        ).fetchone()["ready"]


def execute_ingestion(command):
    from dagster import DagsterInstance

    from pulse.definitions import defs

    with execution_deadline(), DagsterInstance.get() as instance:
        result = defs.resolve_job_def("ingest_all").execute_in_process(
            instance=instance, raise_on_error=False
        )
        if result.success:
            return
        kind = diagnostic_failure_kind(result) if command == "bootstrap" else None
        if kind and diagnostic_publication_ready(kind, result.run_id):
            endpoint = (
                "/api/v1/quality/issues" if kind == "empty" else "/api/v1/quality/file-errors"
            )
            print(
                "Bootstrap: accounted quality failures left a FAILED Dagster run; "
                "a valid publication is available. Starting diagnostics; "
                f"inspect {endpoint}.",
                file=sys.stderr,
            )
            return
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description="Pulse data platform")
    parser.add_argument("command", choices=["bootstrap", "migrate", "ingest"])
    parser.add_argument("--data-dir", help="Directory containing asesorias.csv and drops/")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["PULSE_DATA_DIR"] = args.data_dir
    if args.command in ("bootstrap", "migrate"):
        migrate()
    if args.command in ("bootstrap", "ingest"):
        execute_ingestion(args.command)


if __name__ == "__main__":
    main()
