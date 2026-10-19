import csv
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_parsing import make_csv

from pulse.db import connect
from pulse.ingest import LOCK, ingest
from pulse_api.app import app
from pulse_api.models import FirmMetricPage, NetworkMetricPage

pytestmark = pytest.mark.integration


def source(tmp_path):
    (tmp_path / "drops").mkdir()
    (tmp_path / "asesorias.csv").write_text(
        "id,nombre,cif,provincia,ciudad,fecha_alta,num_empleados,especialidad\n"
        "1,Example,B12345678,Madrid,Madrid,2020-01-01,5,Fiscal\n",
        encoding="utf-8",
    )
    return tmp_path / "drops"


def fingerprint():
    with connect() as conn:
        rows = conn.execute("""SELECT to_jsonb(f)-'published_run_id' AS data
            FROM mart.firm_month f ORDER BY asesoria_id,mes""").fetchall()
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def test_corrections_out_of_order_idempotency_and_lineage(database, tmp_path):
    drops = source(tmp_path)
    (drops / "metricas_2025-11.csv").write_bytes(
        make_csv([{"facturacion_asesoria_eur": "25", "facturacion_otros_eur": ""}], 2)
    )
    ingest(tmp_path)
    # An older delivery discovered later must never overwrite a newer correction.
    (drops / "metricas_2025-10.csv").write_bytes(make_csv([{"facturacion_asesoria_eur": "10"}]))
    ingest(tmp_path)
    before = fingerprint()
    result = ingest(tmp_path)
    assert result["skipped"] == 2
    assert fingerprint() == before
    with connect() as conn:
        assert conn.execute("SELECT facturacion_asesoria_eur AS v FROM mart.firm_month").fetchone()[
            "v"
        ] == Decimal("25")
        assert conn.execute("SELECT count(*) AS n FROM raw.records").fetchone()["n"] == 2
    client = TestClient(app)
    trace = client.get("/api/v1/lineage/1/2025-10").json()
    assert trace["versions"][0]["filename"] == "metricas_2025-11.csv"
    assert len(trace["versions"]) == 2
    assert trace["selected_record_id"] == trace["versions"][0]["id"]


def test_invalid_latest_keeps_previous_with_visible_fallback(database, tmp_path):
    drops = source(tmp_path)
    (drops / "metricas_2025-10.csv").write_bytes(make_csv([{}]))
    (drops / "metricas_2025-11.csv").write_bytes(
        make_csv([{"satisfaccion_media": "99", "facturacion_otros_eur": ""}], 2)
    )
    ingest(tmp_path)
    with connect() as conn:
        assert conn.execute(
            "SELECT has_rejected_newer_delivery AS v FROM mart.firm_month"
        ).fetchone()["v"]
    client = TestClient(app)
    assert client.get("/api/v1/quality/summary").json()["rejected_rows"] == 1
    assert client.get("/api/v1/quality/summary").json()["fallback_rows"] == 1


def test_changed_file_and_bad_header_are_preserved(database, tmp_path):
    drops = source(tmp_path)
    original = drops / "metricas_2025-10.csv"
    original.write_bytes(make_csv([{}]))
    ingest(tmp_path)
    before = fingerprint()
    original.write_bytes(make_csv([{"clientes_activos": "99"}]))
    (drops / "metricas_2025-11.csv").write_bytes(b"broken,header\n1,2\n")
    result = ingest(tmp_path)
    assert result["status"] == "partial"
    assert result["failed"] == 2
    assert fingerprint() == before
    with connect() as conn:
        errors = conn.execute("SELECT content,reason FROM ops.file_errors").fetchall()
        assert len(errors) == 2
        assert any(bytes(row["content"]) == original.read_bytes() for row in errors)
    # The original is immutable; a versioned delivery can explicitly correct it.
    original.write_bytes(make_csv([{}]))
    (drops / "metricas_2025-10__001.csv").write_bytes(make_csv([{"clientes_activos": "99"}]))
    ingest(tmp_path)
    with connect() as conn:
        assert (
            conn.execute("SELECT clientes_activos AS n FROM mart.firm_month").fetchone()["n"] == 99
        )


def test_ingestion_lock_and_failed_master_leave_auditable_run(database, tmp_path):
    source(tmp_path)
    with connect() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK,))
        with pytest.raises(RuntimeError, match="Another ingestion"):
            ingest(tmp_path)
    (tmp_path / "asesorias.csv").write_text("bad\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ingest(tmp_path)
    with connect() as conn:
        assert conn.execute("SELECT status FROM ops.runs").fetchone()["status"] == "failed"


def test_publication_failure_rolls_back_and_retry_uses_archived_rows(
    database, tmp_path, monkeypatch
):
    import importlib

    ingestion_module = importlib.import_module("pulse.ingest")
    drops = source(tmp_path)
    (drops / "metricas_2025-10.csv").write_bytes(make_csv([{}]))
    ingest(tmp_path)
    before = fingerprint()
    (drops / "metricas_2025-10__001.csv").write_bytes(make_csv([{"clientes_activos": "42"}]))
    original = ingestion_module.publish

    def broken(conn, run_id):
        conn.execute("DELETE FROM mart.firm_month")
        raise RuntimeError("Simulated publication failure")

    monkeypatch.setattr(ingestion_module, "publish", broken)
    with pytest.raises(RuntimeError, match="Simulated"):
        ingest(tmp_path)
    assert fingerprint() == before
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM raw.deliveries").fetchone()["n"] == 2
    monkeypatch.setattr(ingestion_module, "publish", original)
    assert ingest(tmp_path)["skipped"] == 2
    with connect() as conn:
        assert (
            conn.execute("SELECT clientes_activos AS n FROM mart.firm_month").fetchone()["n"] == 42
        )


def test_new_month_and_null_contributor_counts(database, tmp_path):
    drops = source(tmp_path)
    (drops / "metricas_2025-10.csv").write_bytes(make_csv([{"horas_trabajadas": ""}]))
    ingest(tmp_path)
    (drops / "metricas_2025-11.csv").write_bytes(make_csv([{"mes": "2025-11"}], 2))
    assert ingest(tmp_path)["loaded"] == 1
    client = TestClient(app)
    months = client.get("/api/v1/network/metrics").json()["items"]
    assert len(months) == 2
    assert months[0]["hours_reporting_firms"] == 0
    assert months[0]["horas_trabajadas"] is None
    assert Decimal(months[1]["facturacion_otros_eur"]) == 10


def independent_reference(root):
    """Independent oracle for the provided fixture; does not call the production parser."""
    latest = {}
    for path in sorted((root / "drops").glob("*.csv")):
        text = path.read_text(encoding="utf-8-sig")
        sep = ";" if ";" in text.splitlines()[0] else ","
        seen = {}
        for raw in csv.DictReader(text.splitlines(), delimiter=sep):
            firm = int(raw["asesoria_id"])
            month = raw["mes"]
            if not 1 <= firm <= 500 or not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", month):
                continue
            score = Decimal(
                raw.get("satisfaccion_cliente", raw.get("satisfaccion_media")).replace(",", ".")
            )
            if not 1 <= score <= 5:
                continue
            money = [
                Decimal((raw.get(k) or "0").replace(",", "."))
                for k in (
                    "facturacion_asesoria_eur",
                    "facturacion_gestion_eur",
                    "facturacion_consultoria_eur",
                    "facturacion_otros_eur",
                )
            ]
            if any(x < 0 for x in money):
                continue
            key = (firm, month)
            value = (sum(money), int(raw["clientes_activos"]))
            if key in seen:
                assert seen[key] == value, "Unexpected conflicting valid fixture row"
            seen[key] = value
        latest.update(seen)
    totals = {}
    for (_, month), (revenue, clients) in latest.items():
        if month not in totals:
            totals[month] = [Decimal(0), 0, 0]
        totals[month][0] += revenue
        totals[month][1] += clients
        totals[month][2] += 1
    return latest, totals


def test_full_fixture_idempotency_reconciliation_and_api(database):
    root = Path("data")
    assert ingest(root)["loaded"] == 36
    before = fingerprint()
    assert ingest(root)["skipped"] == 36
    assert fingerprint() == before
    latest, expected = independent_reference(root)
    assert len(latest) == 17988
    client = TestClient(app)
    summary = client.get("/api/v1/quality/summary").json()
    assert {
        k: summary[k]
        for k in (
            "raw_rows",
            "accepted_rows",
            "rejected_rows",
            "duplicate_rows",
            "curated_rows",
            "missing_firm_months",
        )
    } == {
        "raw_rows": 18087,
        "accepted_rows": 18028,
        "rejected_rows": 28,
        "duplicate_rows": 31,
        "curated_rows": 17988,
        "missing_firm_months": 12,
    }
    response = client.get("/api/v1/network/metrics")
    assert response.status_code == 200
    NetworkMetricPage.model_validate(response.json())
    FirmMetricPage.model_validate(client.get("/api/v1/firms/409/metrics").json())
    assert response.json()["total"] == 36
    for row in response.json()["items"]:
        revenue, clients, firms = expected[row["mes"][:7]]
        assert Decimal(row["revenue_eur"]) == revenue
        assert row["clientes_activos"] == clients
        assert row["reporting_firms"] == firms
    assert client.get("/api/v1/firms").json()["total"] == 500
    assert client.get("/api/v1/firms/9999/metrics").status_code == 404
    assert client.get("/api/v1/network/metrics?start=2025-13").status_code == 422
    assert client.get("/api/v1/network/metrics?start=2025-03&end=2024-01").status_code == 422
    assert client.get("/api/v1/firms?limit=501").status_code == 422
    assert client.get("/api/v1/quality/issues?severity=error").json()["total"] >= 28
    assert client.get("/api/v1/quality/deliveries").json()["total"] == 36
    assert client.get("/api/v1/quality/gaps").json()["total"] == 12
    missing = client.get("/api/v1/firms/409/metrics?start=2023-05&end=2023-05").json()["items"][0]
    assert missing["coverage_status"] == "missing" and missing["revenue_eur"] is None
    assert client.get("/").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.post("/api/v1/firms", json={"id": 1000}).status_code == 405
