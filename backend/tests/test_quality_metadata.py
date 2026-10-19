"""Quality evidence must describe incorporated data, including recovery paths."""

import importlib

import pytest
from fastapi.testclient import TestClient
from test_ingestion import source
from test_parsing import make_csv

from pulse.db import connect
from pulse.ingest import ingest
from pulse_api.app import app

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("existing_publication", [False, True])
def test_recovered_publication_advances_incorporation_but_replay_does_not(
    database, tmp_path, monkeypatch, existing_publication
):
    drops = source(tmp_path)
    client = TestClient(app)
    assert client.get("/api/v1/quality/summary").json()["last_successful_load"] is None
    (drops / "metricas_2025-10.csv").write_bytes(make_csv([{}]))
    if existing_publication:
        ingest(tmp_path)
        (drops / "metricas_2025-10__001.csv").write_bytes(make_csv([{"clientes_activos": "42"}]))
    before = client.get("/api/v1/quality/summary").json()["last_successful_load"]
    module = importlib.import_module("pulse.ingest")
    publish = module.publish

    def broken(conn, run_id):
        publish(conn, run_id)
        raise RuntimeError("Fail after publication membership but before commit")

    monkeypatch.setattr(module, "publish", broken)
    with pytest.raises(RuntimeError, match="before commit"):
        ingest(tmp_path)
    failed = client.get("/api/v1/quality/summary").json()
    assert failed["latest_run"]["status"] == "failed"
    assert failed["last_successful_load"] == before
    monkeypatch.setattr(module, "publish", publish)
    recovered = ingest(tmp_path)
    assert recovered["loaded"] == 0
    assert recovered["skipped"] == (2 if existing_publication else 1)
    after = client.get("/api/v1/quality/summary").json()["last_successful_load"]
    with connect() as conn:
        published_at = conn.execute(
            "SELECT published_at FROM ops.publications WHERE id=%s", (recovered["run_id"],)
        ).fetchone()["published_at"]
    assert after == published_at.isoformat()
    assert after != before
    ingest(tmp_path)
    assert client.get("/api/v1/quality/summary").json()["last_successful_load"] == after


def test_partial_publication_reports_incorporation_without_claiming_a_clean_run(database, tmp_path):
    drops = source(tmp_path)
    (drops / "metricas_2025-10.csv").write_bytes(make_csv([{}]))
    (drops / "metricas_2025-11.csv").write_bytes(b"broken,header\n1,2\n")
    result = ingest(tmp_path)
    summary = TestClient(app).get("/api/v1/quality/summary").json()
    assert result["status"] == "partial"
    assert summary["latest_run"]["status"] == "partial"
    assert summary["file_errors"] == 1
    with connect() as conn:
        published_at = conn.execute(
            "SELECT published_at FROM ops.publications WHERE id=%s", (result["run_id"],)
        ).fetchone()["published_at"]
    assert summary["last_successful_load"] == published_at.isoformat()
    assert ingest(tmp_path)["loaded"] == 0
    replay = TestClient(app).get("/api/v1/quality/summary").json()
    assert replay["last_successful_load"] == summary["last_successful_load"]


def test_issue_filter_rejects_invalid_ids_instead_of_returning_all_firms(database, tmp_path):
    drops = source(tmp_path)
    (drops / "metricas_2025-10.csv").write_bytes(
        make_csv([{"satisfaccion_cliente": "99"}, {"asesoria_id": "2"}])
    )
    ingest(tmp_path)
    client = TestClient(app)
    for firm_id in ("0", "-1", "2147483648", "1.5"):
        assert client.get(f"/api/v1/quality/issues?firm_id={firm_id}").status_code == 422
    all_issues = client.get("/api/v1/quality/issues").json()
    filtered = client.get("/api/v1/quality/issues?firm_id=1").json()
    assert 0 < filtered["total"] < all_issues["total"]
    assert all(row["normalized"]["asesoria_id"] == 1 for row in filtered["items"])
    assert client.get("/api/v1/quality/issues?firm_id=999").json()["total"] == 0
