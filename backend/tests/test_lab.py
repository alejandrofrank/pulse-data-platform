import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_ingestion import source
from test_parsing import make_csv

from pulse.db import connect
from pulse.ingest import LOCK as INGEST_LOCK
from pulse.ingest import ingest
from pulse_api.lab import Playback, validate_target
from pulse_api.lab_api import app

pytestmark = pytest.mark.integration
HEADERS = {"X-Pulse-Lab": "playback-v1"}
OCT = "metricas_2025-10.csv"
NOV = "metricas_2025-11.csv"


@pytest.fixture
def lab_root(database, tmp_path, monkeypatch):
    drops = source(tmp_path)
    (drops / OCT).write_bytes(make_csv([{"facturacion_asesoria_eur": "10"}]))
    (drops / NOV).write_bytes(
        make_csv([{"facturacion_asesoria_eur": "25", "facturacion_otros_eur": ""}], 2)
    )
    monkeypatch.setenv("LAB_DATABASE_URL", database)
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path))
    return tmp_path


def request(client, names, delay=0, request_id=None):
    return client.post(
        "/api/lab/play",
        headers=HEADERS,
        json={
            "request_id": str(request_id or uuid4()),
            "filenames": names,
            "interval_ms": delay,
        },
    )


def wait_for(client, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get("/api/lab/state").json()
        if predicate(state):
            return state
        time.sleep(0.03)
    raise AssertionError(f"Lab condition timed out: {state}")


def finished(client):
    return wait_for(client, lambda s: s["job"]["status"] in ("success", "failed"))


def test_selected_ingestion_never_falls_back_to_all_files(database, lab_root):
    for invalid in ([], ["../asesorias.csv"], [OCT, OCT], ["missing.csv"]):
        with pytest.raises(ValueError):
            ingest(lab_root, file_names=invalid, database_url=database)
    assert ingest(lab_root, file_names=[OCT], database_url=database)["loaded"] == 1
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM raw.deliveries").fetchone()["n"] == 1


def test_playback_corrections_replays_and_request_idempotency(database, lab_root):
    with TestClient(app) as client:
        initial = client.get("/api/lab/state").json()
        assert initial["counts"]["raw_rows"] == 0
        assert len(initial["catalog"]) == 2
        job_id = uuid4()
        assert request(client, [OCT, NOV], request_id=job_id).status_code == 200
        first = finished(client)
        assert first["job"]["status"] == "success"
        assert first["counts"]["files"] == 2
        assert first["counts"]["published_rows"] == 1
        assert first["job"]["steps"][1]["evidence"]["updated"] == 1
        assert all(c["passed"] for c in first["checks"])
        assert request(client, [OCT, NOV], request_id=job_id).status_code == 200
        assert request(client, [NOV], request_id=job_id).status_code == 409
        path = lab_root / "drops" / NOV
        content = path.read_bytes()
        path.unlink()
        try:
            # A retry returns the accepted job even when its inputs are no longer available.
            assert request(client, [OCT, NOV], request_id=job_id).json()["id"] == str(job_id)
        finally:
            path.write_bytes(content)
        # A clock adjustment must not make the previous run look newer than its successor.
        with connect(database) as conn:
            conn.execute(
                "UPDATE lab.jobs SET created_at=now()+interval '1 day' WHERE id=%s", (job_id,)
            )
        assert request(client, [OCT, NOV]).status_code == 200
        replay = finished(client)
        assert replay["business_hash"] == first["business_hash"]
        assert replay["counts"] == first["counts"]
        assert all(
            s["status"] == "skipped" and s["evidence"]["idempotent"] for s in replay["job"]["steps"]
        )
        assert len(replay["jobs"]) == 2
        assert client.get("/api/v1/lineage/1/2025-10").json()["versions"][0]["filename"] == NOV


def test_out_of_order_pause_restart_resume_and_reset(database, lab_root):
    with TestClient(app) as client:
        created = request(client, [NOV, OCT], delay=3000).json()
        job_id = created["id"]
        wait_for(client, lambda s: s["job"]["cursor"] == 1)
        assert request(client, [OCT]).status_code == 409
        assert (
            client.post(
                "/api/lab/reset", headers=HEADERS, json={"confirmation": "RESET LAB"}
            ).status_code
            == 409
        )
        client.post(f"/api/lab/jobs/{job_id}/pause", headers=HEADERS)
        wait_for(client, lambda s: s["job"]["status"] == "paused")
    # Simulate a process killed after a step was committed but before the next completed.
    with connect(database) as conn:
        conn.execute("UPDATE lab.jobs SET status='running' WHERE id=%s", (job_id,))
        conn.execute(
            "UPDATE lab.steps SET status='running' WHERE job_id=%s AND position=1", (job_id,)
        )
    with TestClient(app) as client:
        recovered = client.get("/api/lab/state").json()
        assert recovered["job"]["status"] == "paused" and recovered["job"]["cursor"] == 1
        assert client.post(f"/api/lab/jobs/{job_id}/resume", headers=HEADERS).status_code == 200
        result = finished(client)
        assert result["job"]["status"] == "success"
        assert result["job"]["steps"][1]["evidence"]["updated"] == 0
        assert all(c["passed"] for c in result["checks"])
        assert client.get("/api/v1/lineage/1/2025-10").json()["versions"][0]["filename"] == NOV
        assert (
            client.post(
                "/api/lab/reset", headers=HEADERS, json={"confirmation": "RESET LAB"}
            ).status_code
            == 200
        )
        empty = client.get("/api/lab/state").json()
        assert empty["counts"]["files"] == 0 and empty["job"] is None
        assert client.get("/api/v1/firms").json()["total"] == 1
        assert (lab_root / "drops" / OCT).exists()


def test_topline_matches_reporting_after_out_of_order_load_replay_and_missing_month(lab_root):
    (lab_root / "drops" / NOV).write_bytes(
        make_csv(
            [{"mes": "2025-11", "clientes_activos": "20", "facturacion_asesoria_eur": "25"}], 2
        )
    )
    december = "metricas_2025-12.csv"
    (lab_root / "drops" / december).write_bytes(
        make_csv([{"mes": "2025-12", "satisfaccion_media": "8,4"}], 2)
    )
    with TestClient(app) as client:
        assert client.get("/api/lab/state").json()["months"] == []
        assert request(client, [NOV, OCT]).status_code == 200
        loaded = finished(client)
        assert loaded["job"]["status"] == "success"
        assert [row["mes"] for row in loaded["months"]] == ["2025-10-01", "2025-11-01"]
        assert loaded["months"][-1] == {
            "mes": "2025-11-01",
            "revenue_eur": "55.00",
            "clientes_activos": 20,
            "reporting_firms": 1,
            "expected_firms": 1,
            "missing_firms": 0,
            "coverage_pct": "100.00",
        }
        reported = client.get("/api/v1/network/metrics").json()["items"]
        for actual, expected in zip(loaded["months"], reported, strict=True):
            assert actual == {key: expected[key] for key in actual}
        assert request(client, [OCT, NOV]).status_code == 200
        replay = finished(client)
        assert replay["months"] == loaded["months"]
        assert replay["business_hash"] == loaded["business_hash"]
        assert request(client, [december]).status_code == 200
        missing = finished(client)["months"][-1]
        assert missing["mes"] == "2025-12-01"
        assert missing["revenue_eur"] is None
        assert missing["clientes_activos"] is None
        assert missing["reporting_firms"] == 0
        assert missing["expected_firms"] == missing["missing_firms"] == 1
        assert missing["coverage_pct"] == "0.00"


def test_mutation_failure_is_visible_and_retry_recovers(lab_root):
    with TestClient(app) as client:
        request(client, [OCT])
        original = finished(client)
        path = lab_root / "drops" / OCT
        content = path.read_bytes()
        path.write_bytes(make_csv([{"clientes_activos": "99"}]))
        job_id = request(client, [OCT]).json()["id"]
        failed = finished(client)
        assert failed["job"]["status"] == "failed"
        assert failed["counts"]["file_errors"] == 1
        assert failed["file_errors"][0]["filename"] == OCT
        assert failed["business_hash"] == original["business_hash"]
        assert failed["catalog"][0]["status"] == "changed"
        path.write_bytes(content)
        client.post(f"/api/lab/jobs/{job_id}/resume", headers=HEADERS)
        recovered = finished(client)
        assert recovered["job"]["status"] == "success"
        assert recovered["counts"]["file_errors"] == 0
        assert recovered["job"]["steps"][0]["evidence"]["idempotent"]


def test_lab_request_boundaries_and_single_worker(database, lab_root):
    with pytest.raises(ValueError, match="forbidden"):
        validate_target("postgresql://user@db/pulse")
    with TestClient(app) as client:
        body = {"request_id": str(uuid4()), "filenames": [OCT]}
        assert client.post("/api/lab/play", json=body).status_code == 403
        assert (
            client.post(
                "/api/lab/play", headers={**HEADERS, "Origin": "https://example.org"}, json=body
            ).status_code
            == 403
        )
        assert request(client, []).status_code == 422
        assert request(client, ["../asesorias.csv"]).status_code == 422
        assert (
            client.post(
                "/api/lab/reset", headers=HEADERS, json={"confirmation": "wrong"}
            ).status_code
            == 422
        )
        second = Playback(database, lab_root)
        with pytest.raises(ValueError, match="already running"):
            second.start()
        assert client.get("/api/lab/health").status_code == 200
        with connect(database) as writer:
            writer.execute("SELECT pg_advisory_lock(%s)", (INGEST_LOCK,))
            assert (
                client.post(
                    "/api/lab/reset", headers=HEADERS, json={"confirmation": "RESET LAB"}
                ).status_code
                == 409
            )
        client.app.state.playback.stop()
        assert request(client, [OCT]).status_code == 409
        assert client.get("/api/lab/health").status_code == 503
        assert client.get(f"/api/lab/jobs/{uuid4()}").status_code == 404
        assert (
            client.get("/api/lab/state", headers={"Host": "untrusted.example"}).status_code == 400
        )


def test_lab_retry_repairs_archived_but_unpublished_delivery(lab_root, monkeypatch):
    import importlib

    module = importlib.import_module("pulse.ingest")
    original = module.publish
    with TestClient(app) as client:
        request(client, [OCT])
        first = finished(client)

        def broken(conn, run_id):
            conn.execute("DELETE FROM mart.firm_month")
            raise RuntimeError("Interrupted publication")

        monkeypatch.setattr(module, "publish", broken)
        job_id = request(client, [NOV]).json()["id"]
        failed = finished(client)
        assert failed["job"]["status"] == "failed"
        assert failed["counts"]["files"] == 2
        assert failed["business_hash"] == first["business_hash"]
        monkeypatch.setattr(module, "publish", original)
        client.post(f"/api/lab/jobs/{job_id}/resume", headers=HEADERS)
        recovered = finished(client)
        assert recovered["job"]["status"] == "success"
        proof = recovered["job"]["steps"][0]["evidence"]
        assert proof["run"]["skipped"] == 1 and proof["recovery"]
        assert not proof["idempotent"] and proof["updated"] == 1
        assert all(check["passed"] for check in recovered["checks"])
        assert client.get(f"/api/lab/jobs/{job_id}").json()["steps"][0]["evidence"] == proof
