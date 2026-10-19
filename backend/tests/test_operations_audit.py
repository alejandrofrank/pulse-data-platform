"""Operations regressions. PostgreSQL cases are run only by the coordinated DB suite."""

import logging
import os
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from dagster import DagsterInstance, DagsterRunStatus, Failure
from dagster._daemon.monitoring import run_monitoring
from dagster._daemon.run_coordinator.queued_run_coordinator_daemon import QueuedRunCoordinatorDaemon
from test_parsing import make_csv

from pulse import cli
from pulse import definitions as pipeline
from pulse.db import connect
from pulse.ingest import ingest


def monitoring_config():
    path = Path(__file__).resolve().parents[1] / "00_infrastructure/dagster/dagster.yaml"
    if not path.is_file():  # The packaged test image stores configuration in DAGSTER_HOME.
        path = Path(os.environ["DAGSTER_HOME"]) / "dagster.yaml"
    config = yaml.safe_load(path.read_text())
    return {key: config[key] for key in ("run_monitoring", "run_coordinator")}


@pytest.fixture
def instance(tmp_path, monkeypatch):
    with DagsterInstance.local_temp(
        str(tmp_path), overrides={"telemetry": {"enabled": False}}
    ) as value:
        monkeypatch.setattr(DagsterInstance, "get", lambda: nullcontext(value))
        yield value


def test_execution_deadline_kills_process_and_monitor_releases_orphan(tmp_path, monkeypatch):
    """Real pinned monitor, real interrupted execute_in_process, isolated SQLite only."""
    config = monitoring_config()
    assert config["run_monitoring"]["enabled"]
    assert config["run_monitoring"]["max_resume_run_attempts"] == 0
    for key in ("start_timeout_seconds", "max_runtime_seconds"):
        assert config["run_monitoring"][key] > cli.CLI_EXECUTION_BUDGET_SECONDS
    script = """
import sys, time
from dagster import DagsterInstance, job, op
from pulse.cli import execution_deadline
@op
def interrupted():
    with execution_deadline(0.1):
        time.sleep(30)
@job(name="ingest_all")
def manual_job():
    interrupted()
with DagsterInstance.local_temp(
    sys.argv[1], overrides={"telemetry": {"enabled": False}}
) as instance:
    manual_job.execute_in_process(instance=instance)
"""
    process = subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert process.returncode == 124, process.stderr
    with DagsterInstance.local_temp(str(tmp_path), overrides=config) as value:
        record = value.get_run_records()[0]
        assert record.dagster_run.status == DagsterRunStatus.STARTED
        assert record.dagster_run.remote_job_origin is None
        assert not value.run_launcher.supports_check_run_worker_health
        queue = QueuedRunCoordinatorDaemon(interval_seconds=1)
        assert len(queue._get_in_progress_run_records(value)) == 1
        assert not queue._get_runs_to_dequeue(value, value.get_concurrency_config(), None)
        budget = value.run_monitoring_max_runtime_seconds
        clock = record.start_time + budget - 1
        monkeypatch.setattr(run_monitoring, "get_current_timestamp", lambda: clock)
        logger = logging.getLogger(__name__)
        run_monitoring.monitor_started_run(value, None, record, logger)
        assert value.get_run_by_id(record.dagster_run.run_id).status == DagsterRunStatus.STARTED
        clock += 2
        run_monitoring.monitor_started_run(value, None, record, logger)
        assert value.get_run_by_id(record.dagster_run.run_id).status == DagsterRunStatus.FAILURE
        assert not queue._get_in_progress_run_records(value)


def test_execution_deadline_is_cancelled_after_completion():
    process = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import time; from pulse.cli import execution_deadline; "
            "guard=execution_deadline(0.1); guard.__enter__(); guard.__exit__(None,None,None); "
            "time.sleep(0.2)",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert process.returncode == 0, process.stderr


@pytest.mark.parametrize(
    "command,ready", [("bootstrap", True), ("bootstrap", False), ("ingest", True)]
)
def test_real_quarantine_failure_only_allows_verified_bootstrap(
    instance, monkeypatch, command, ready, capsys
):
    monkeypatch.setattr(
        pipeline,
        "ingest",
        lambda **kwargs: {"status": "partial", "failed": 1, "run_id": "publication"},
    )
    verify = Mock(return_value=ready)
    monkeypatch.setattr(cli, "diagnostic_publication_ready", verify)
    if command == "bootstrap" and ready:
        cli.execute_ingestion(command)
        assert "inspect /api/v1/quality/file-errors." in capsys.readouterr().err
    else:
        with pytest.raises(SystemExit, match="1"):
            cli.execute_ingestion(command)
    run = instance.get_runs()[0]
    assert run.status == DagsterRunStatus.FAILURE
    if command == "bootstrap":
        verify.assert_called_once_with("quarantine", run.run_id)
    else:
        verify.assert_not_called()


def test_real_empty_check_has_explicit_marker_and_failed_run(instance, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "ingest", lambda **kwargs: {"status": "success"})
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = {"n": 0}
    monkeypatch.setattr(pipeline, "connect", lambda: nullcontext(conn))
    verify = Mock(return_value=True)
    monkeypatch.setattr(cli, "diagnostic_publication_ready", verify)
    cli.execute_ingestion("bootstrap")
    assert "inspect /api/v1/quality/issues." in capsys.readouterr().err
    run = instance.get_runs()[0]
    assert run.status == DagsterRunStatus.FAILURE
    verify.assert_called_once_with("empty", run.run_id)


@pytest.mark.parametrize("missing_marker", [False, True])
def test_arbitrary_check_failure_is_not_quality_only(missing_marker):
    evaluation = SimpleNamespace(
        asset_key=SimpleNamespace(path=["monthly_metrics"]),
        check_name="publication_integrity",
        passed=False,
        metadata={
            "curated_rows": SimpleNamespace(value=0),
            "invalid_sources": SimpleNamespace(value=0),
            **(
                {}
                if missing_marker
                else {pipeline.EMPTY_PUBLICATION_FAILURE: SimpleNamespace(value=True)}
            ),
        },
    )
    event = SimpleNamespace(
        step_key="monthly_metrics_publication_integrity",
        event_specific_data=SimpleNamespace(
            user_failure_data=None,
            error=SimpleNamespace(
                cls_name="DagsterAssetCheckFailedError" if missing_marker else "RuntimeError"
            ),
        ),
    )
    result = Mock()
    result.get_run_failure_event.return_value.event_specific_data.error = None
    result.get_step_failure_events.return_value = [event]
    result.get_asset_check_evaluations.return_value = [evaluation]
    assert cli.diagnostic_failure_kind(result) is None


def test_run_level_error_prevents_diagnostic_acceptance():
    result = Mock()
    result.get_run_failure_event.return_value.event_specific_data.error = RuntimeError("storage")
    assert cli.diagnostic_failure_kind(result) is None
    result.get_step_failure_events.assert_not_called()


def test_unexpected_asset_failure_remains_fatal(instance, monkeypatch):
    def fail(**kwargs):
        raise Failure("Unrelated infrastructure failure", allow_retries=False)

    monkeypatch.setattr(pipeline, "ingest", fail)
    verify = Mock()
    monkeypatch.setattr(cli, "diagnostic_publication_ready", verify)
    with pytest.raises(SystemExit, match="1"):
        cli.execute_ingestion("bootstrap")
    verify.assert_not_called()
    assert instance.get_runs()[0].status == DagsterRunStatus.FAILURE


def test_publication_probe_errors_are_not_accepted(instance, monkeypatch):
    monkeypatch.setattr(pipeline, "ingest", lambda **kwargs: {"status": "partial", "failed": 1})
    monkeypatch.setattr(cli, "connect", Mock(side_effect=RuntimeError("database unavailable")))
    with pytest.raises(RuntimeError, match="database unavailable"):
        cli.execute_ingestion("bootstrap")


def test_migration_failure_does_not_start_ingestion(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pulse", "bootstrap"])
    monkeypatch.setattr(cli, "migrate", Mock(side_effect=RuntimeError("migration failed")))
    execute = Mock()
    monkeypatch.setattr(cli, "execute_ingestion", execute)
    with pytest.raises(RuntimeError, match="migration failed"):
        cli.main()
    execute.assert_not_called()


def source(root, outcome):
    (root / "drops").mkdir()
    (root / "asesorias.csv").write_text(
        "id,nombre,cif,provincia,ciudad,fecha_alta,num_empleados,especialidad\n"
        "1,Example,B12345678,Madrid,Madrid,2020-01-01,5,Fiscal\n",
        encoding="utf-8",
    )
    if outcome == "rejected_rows":
        (root / "drops/metricas_2025-10.csv").write_bytes(make_csv([{"clientes_activos": "-1"}]))
    else:
        (root / "drops/metricas_2025-11.csv").write_bytes(b"broken,header\n1,2\n")
        if outcome == "mixed":
            (root / "drops/metricas_2025-10.csv").write_bytes(make_csv([{}]))


@pytest.mark.integration
@pytest.mark.parametrize(
    "outcome,expected_rows", [("mixed", 1), ("bad_files", 0), ("rejected_rows", 0)]
)
@pytest.mark.parametrize("command", ["bootstrap", "ingest"])
def test_cli_quality_outcomes_keep_failed_run_and_diagnostics(
    database, instance, tmp_path, monkeypatch, outcome, expected_rows, command
):
    source(tmp_path, outcome)
    monkeypatch.setattr(sys, "argv", ["pulse", command, "--data-dir", str(tmp_path)])
    if command == "bootstrap":
        cli.main()
    else:
        with pytest.raises(SystemExit, match="1"):
            cli.main()
    run = instance.get_runs()[0]
    assert run.status == DagsterRunStatus.FAILURE
    with connect() as conn:
        assert (
            conn.execute("SELECT count(*) AS n FROM mart.firm_month").fetchone()["n"]
            == expected_rows
        )
        publication = conn.execute("SELECT * FROM ops.publications").fetchone()
        assert publication["record_count"] == expected_rows
        if outcome == "rejected_rows":
            assert conn.execute("SELECT status FROM raw.records").fetchone()["status"] == "rejected"
        else:
            assert conn.execute("SELECT count(*) AS n FROM ops.file_errors").fetchone()["n"] == 1
    # Real HTTP handlers using the same database; no network or model request.
    from fastapi.testclient import TestClient

    from pulse_api.app import app

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/api/v1/quality/summary")
        assert response.status_code == 200 and response.json()["curated_rows"] == expected_rows
        assert client.get("/api/v1/quality/file-errors").status_code == 200


@pytest.mark.integration
@pytest.mark.parametrize("damage", ["count", "source", "different_run"])
def test_bootstrap_probe_rejects_inconsistent_publication(database, tmp_path, damage):
    source(tmp_path, "mixed")
    run_id = "diagnostic-test"
    ingest(tmp_path, dagster_run_id=run_id)
    assert cli.diagnostic_publication_ready("quarantine", run_id)
    with connect() as conn:
        if damage == "count":
            conn.execute("UPDATE ops.publications SET record_count=99")
        elif damage == "source":
            conn.execute("UPDATE raw.records SET status='rejected'")
        else:
            conn.execute("UPDATE ops.runs SET dagster_run_id='different'")
    assert not cli.diagnostic_publication_ready("quarantine", run_id)


@pytest.mark.integration
def test_empty_publication_with_accepted_sources_is_not_quality_only(database, tmp_path):
    source(tmp_path, "rejected_rows")
    run_id = "diagnostic-test"
    ingest(tmp_path, dagster_run_id=run_id)
    assert cli.diagnostic_publication_ready("empty", run_id)
    with connect() as conn:
        conn.execute("UPDATE raw.records SET status='accepted'")
    assert not cli.diagnostic_publication_ready("empty", run_id)
