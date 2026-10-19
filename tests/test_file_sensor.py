import json

import pytest
from dagster import DagsterInstance, DagsterRunStatus, build_sensor_context
from test_ingestion import source
from test_parsing import make_csv

from pulse.db import connect
from pulse.definitions import csv_delivery_sensor, defs
from pulse.ingest import ingest

pytestmark = pytest.mark.integration
OCT = "metricas_2025-10.csv"
NOV = "metricas_2025-11.csv"


@pytest.fixture
def environment(database, tmp_path, monkeypatch):
    drops = source(tmp_path)
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path))
    (tmp_path / "dagster").mkdir()
    with DagsterInstance.local_temp(str(tmp_path / "dagster")) as instance:
        yield instance, drops


def tick(instance, cursor=None):
    with build_sensor_context(
        instance=instance, cursor=cursor, definitions=defs, sensor_name="csv_delivery_sensor"
    ) as context:
        return csv_delivery_sensor.evaluate_tick(context)


def selected(result):
    return result.run_requests[0].run_config["ops"]["monthly_metrics"]["config"]


def test_stable_file_starts_selected_dagster_ingestion_and_then_skips(environment, database):
    instance, drops = environment
    (drops / OCT).write_bytes(make_csv([{}]))
    first = tick(instance)
    assert not first.run_requests
    second = tick(instance, first.cursor)
    assert len(second.run_requests) == 1 and selected(second)["file_names"] == [OCT]
    # Re-evaluation of the same persisted observation gives the same Dagster deduplication key.
    assert tick(instance, second.cursor).run_requests[0].run_key == second.run_requests[0].run_key
    assert "signature" in json.loads(second.cursor)["files"][OCT]
    result = defs.resolve_job_def("ingest_all").execute_in_process(
        instance=instance, run_config=second.run_requests[0].run_config
    )
    assert result.success
    assert not tick(instance, second.cursor).run_requests
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM mart.firm_month").fetchone()["n"] == 1
        assert (
            conn.execute("SELECT dagster_run_id FROM ops.runs").fetchone()["dagster_run_id"]
            == result.run_id
        )


def test_partial_files_wait_and_only_stable_csvs_are_selected(environment):
    instance, drops = environment
    (drops / OCT).write_bytes(make_csv([{}]))
    (drops / "partial.tmp").write_text("unfinished")
    first = tick(instance)
    (drops / OCT).write_bytes(make_csv([{"clientes_activos": "123"}]))
    changing = tick(instance, first.cursor)
    assert not changing.run_requests
    (drops / NOV).write_bytes(make_csv([{}], 2))
    ready = tick(instance, changing.cursor)
    assert selected(ready)["file_names"] == [OCT]
    ready_both = tick(instance, ready.cursor)
    assert selected(ready_both)["file_names"] == [OCT, NOV]


def test_pending_runs_defer_without_forgetting_deliveries(environment):
    instance, drops = environment
    (drops / OCT).write_bytes(make_csv([{}]))
    first = tick(instance)
    run = instance.create_run_for_job(
        defs.resolve_job_def("ingest_all"), status=DagsterRunStatus.NOT_STARTED
    )
    deferred = tick(instance, first.cursor)
    assert not deferred.run_requests and "queued or active" in deferred.skip_message
    instance.report_run_canceled(run)
    assert selected(tick(instance, deferred.cursor))["file_names"] == [OCT]


def test_changed_after_discovery_is_not_archived_and_new_contents_get_new_key(
    environment, database
):
    instance, drops = environment
    (drops / OCT).write_bytes(make_csv([{}]))
    first = tick(instance)
    ready = tick(instance, first.cursor)
    config = selected(ready)
    (drops / OCT).write_bytes(make_csv([{"clientes_activos": "123"}]))
    with pytest.raises(ValueError, match="changed after sensor inspection"):
        ingest(file_names=config["file_names"], expected_sha256=config["expected_sha256"])
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM raw.deliveries").fetchone()["n"] == 0
    changed = tick(instance, ready.cursor)
    assert not changed.run_requests
    new = tick(instance, changed.cursor)
    assert new.run_requests[0].run_key != ready.run_requests[0].run_key


def test_file_quarantine_does_not_loop_and_restoring_original_resolves_it(environment, database):
    instance, drops = environment
    path = drops / OCT
    original = make_csv([{}])
    path.write_bytes(original)
    ingest()
    first = tick(instance)
    assert not tick(instance, first.cursor).run_requests
    path.write_bytes(make_csv([{"clientes_activos": "999"}]))
    changed = tick(instance, first.cursor)
    ready = tick(instance, changed.cursor)
    result = defs.resolve_job_def("ingest_all").execute_in_process(
        instance=instance, run_config=ready.run_requests[0].run_config, raise_on_error=False
    )
    assert not result.success
    assert not any(event.event_type_value == "STEP_UP_FOR_RETRY" for event in result.all_events)
    assert not tick(instance, ready.cursor).run_requests
    path.write_bytes(original)
    restoring = tick(instance, ready.cursor)
    restored = tick(instance, restoring.cursor)
    config = selected(restored)
    assert (
        ingest(file_names=config["file_names"], expected_sha256=config["expected_sha256"])[
            "skipped"
        ]
        == 1
    )
    with connect(database) as conn:
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM ops.file_errors WHERE resolved_at IS NULL"
            ).fetchone()["n"]
            == 0
        )
    assert not tick(instance, restored.cursor).run_requests


def test_unavailable_directory_is_visible_and_incomplete_checksum_selection_rejected(environment):
    instance, drops = environment
    with pytest.raises(ValueError, match="Expected checksums"):
        ingest(expected_sha256={OCT: "abc"})
    drops.rmdir()
    with pytest.raises(Exception, match="unavailable"):
        tick(instance)


def test_transient_publication_failure_retries_archived_data(environment, database, monkeypatch):
    import importlib

    module = importlib.import_module("pulse.ingest")
    original = module.publish
    attempts = 0

    def fail_once(conn, run_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Transient publication failure")
        original(conn, run_id)

    instance, drops = environment
    (drops / OCT).write_bytes(make_csv([{}]))
    first = tick(instance)
    ready = tick(instance, first.cursor)
    monkeypatch.setattr(module, "publish", fail_once)
    result = defs.resolve_job_def("ingest_all").execute_in_process(
        instance=instance, run_config=ready.run_requests[0].run_config
    )
    assert result.success and attempts == 2
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM raw.deliveries").fetchone()["n"] == 1
        assert (
            conn.execute("SELECT count(*) AS n FROM ops.runs WHERE status='failed'").fetchone()["n"]
            == 1
        )
        assert (
            conn.execute("SELECT files_skipped FROM ops.runs WHERE status='success'").fetchone()[
                "files_skipped"
            ]
            == 1
        )
