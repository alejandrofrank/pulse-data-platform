"""Keep policy-1 raw evidence stable while recovering rejected-row identity.

Pure tests verify both parsers without a database. Integration cases require the
parent-coordinated disposable PostgreSQL suite, including migration 005.
"""

import csv
import io
from collections import Counter
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from pulse.parsing import POLICY_VERSION, parse_drop, parse_master
from pulse_api.reconciliation import verify_delivery

HEADER = (
    "asesoria_id,mes,clientes_activos,clientes_nuevos,clientes_baja,declaraciones_renta,"
    "declaraciones_iva,declaraciones_sociedades,declaraciones_otros,facturacion_asesoria_eur,"
    "facturacion_gestion_eur,facturacion_consultoria_eur,horas_trabajadas,consultas_recibidas,"
    "consultas_resueltas,incidencias_aeat,documentos_procesados,satisfaccion_cliente"
).split(",")
FIRMS = {1: {"fecha_alta": date(2020, 5, 20)}}
MONTH = date(2025, 10, 1)


def delivery(*changes, version=1, shape=None):
    fields = list(HEADER)
    if version == 2:
        fields.insert(12, "facturacion_otros_eur")
        fields[-1] = "satisfaccion_media"
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter="," if version == 1 else ";")
    writer.writerow(fields)
    for change in changes:
        row = dict.fromkeys(fields, "10")
        row.update(asesoria_id="1", mes="2025-10")
        row[fields[-1]] = "4"
        if version == 2:
            row["facturacion_otros_eur"] = ""
        row.update(change)
        cells = [row[field] for field in fields]
        if shape == "extra":
            cells.append("unexpected")
        elif shape == "missing_final":
            cells.pop()
        elif shape == "key_only":
            cells = cells[:2]
        elif shape == "id_only":
            cells = cells[:1]
        else:
            assert shape is None
        writer.writerow(cells)
    return output.getvalue().encode("utf-8")


def assert_parser_parity(content, filename="metricas_2025-11.csv", firms=None):
    firms = FIRMS if firms is None else firms
    version, rows = parse_drop(content, filename, firms)
    verified = verify_delivery(content, filename, firms)
    assert verified["schema_version"] == version
    assert list(verified["records"]) == [row.line for row in rows]
    for row in rows:
        independent = verified["records"][row.line]
        assert independent["payload"] == row.raw
        assert independent["normalized"] == row.values
        assert independent["status"] == row.status
        assert independent["issue_codes"] == [issue.code for issue in row.issues]
    return rows


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("shape", ["extra", "missing_final", "key_only", "id_only"])
def test_malformed_rows_keep_legacy_normalization_and_parser_parity(version, shape):
    rows = assert_parser_parity(
        delivery(
            {"asesoria_id": " 001 ", "facturacion_asesoria_eur": "999"},
            version=version,
            shape=shape,
        )
    )
    row = rows[0]
    assert POLICY_VERSION == "1"
    assert row.status == "rejected"
    assert row.values == {}  # Do not silently reinterpret existing policy-1 archives.
    assert [issue.code for issue in row.issues] == ["column_count"]
    assert row.raw["asesoria_id"] == " 001 "
    assert row.raw["mes"] == (None if shape == "id_only" else "2025-10")


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    "change",
    [
        {"asesoria_id": "999"},
        {"asesoria_id": "0"},
        {"asesoria_id": "-1"},
        {"asesoria_id": "1.0"},
        {"asesoria_id": "x"},
        {"asesoria_id": ""},
        {"asesoria_id": "9" * 100},
        {"mes": ""},
        {"mes": "2025-13"},
        {"mes": "2025-1"},
        {"mes": "0000-01"},
        {"mes": "2025-12"},
        {"mes": "2020-04"},
        {"mes": "2025-10-01"},
        {"asesoria_id": "x", "mes": "invalid"},
    ],
)
def test_malformed_bad_identity_never_creates_normalized_keys(version, change):
    row = assert_parser_parity(delivery(change, version=version, shape="extra"))[0]
    assert row.status == "rejected"
    assert row.values == {}
    assert [issue.code for issue in row.issues] == ["column_count"]


@pytest.mark.parametrize("version", [1, 2])
def test_malformed_metrics_never_compete_with_a_valid_row(version):
    content = delivery({}, version=version)
    content += delivery({"facturacion_asesoria_eur": "999"}, version=version, shape="extra").split(
        b"\r\n", 1
    )[1]
    accepted, rejected = assert_parser_parity(content)
    assert [accepted.status, rejected.status] == ["accepted", "rejected"]
    assert accepted.values["facturacion_asesoria_eur"] == Decimal("10")
    assert rejected.values == {}


def test_supplied_fixture_still_matches_independent_policy_one_parser():
    root = Path(__file__).resolve().parents[2] / "data"
    firms = parse_master((root / "asesorias.csv").read_bytes())
    statuses = Counter()
    for path in sorted((root / "drops").glob("*.csv")):
        rows = assert_parser_parity(path.read_bytes(), path.name, firms)
        statuses.update(row.status for row in rows)
    assert statuses == {"accepted": 18028, "rejected": 28, "duplicate": 31}


def source(tmp_path):
    drops = tmp_path / "drops"
    drops.mkdir()
    (tmp_path / "asesorias.csv").write_text(
        "id,nombre,cif,provincia,ciudad,fecha_alta,num_empleados,especialidad\n"
        "1,Example,B12345678,Madrid,Madrid,2020-05-20,5,Fiscal\n",
        encoding="utf-8",
    )
    return drops


@pytest.mark.integration
@pytest.mark.parametrize("shape", ["extra", "missing_final", "key_only"])
@pytest.mark.parametrize(
    "filename,version", [("metricas_2025-11.csv", 2), ("metricas_2025-10__001.csv", 1)]
)
def test_newer_malformed_correction_is_flagged_and_visible(
    database, tmp_path, shape, filename, version
):
    from fastapi.testclient import TestClient

    from pulse.db import connect
    from pulse.ingest import ingest
    from pulse_api.app import app

    drops = source(tmp_path)
    (drops / "metricas_2025-10.csv").write_bytes(delivery({}))
    ingest(tmp_path)
    with connect() as conn:
        original = conn.execute("SELECT source_record_id FROM mart.firm_month").fetchone()
    (drops / filename).write_bytes(
        delivery(
            {"asesoria_id": " 001 ", "facturacion_asesoria_eur": "999"},
            version=version,
            shape=shape,
        )
    )
    ingest(tmp_path)
    with connect() as conn:
        winner = conn.execute("SELECT * FROM mart.firm_month").fetchone()
        assert winner["source_record_id"] == original["source_record_id"]
        assert winner["facturacion_asesoria_eur"] == Decimal("10")
        assert winner["has_rejected_newer_delivery"] is True
        malformed = conn.execute(
            """SELECT r.id,r.normalized,r.status FROM raw.records r
            JOIN raw.deliveries d ON d.id=r.delivery_id WHERE d.filename=%s""",
            (filename,),
        ).fetchone()
        assert malformed["normalized"] == {} and malformed["status"] == "rejected"
        # Model a pre-fix reporting projection; recovery must work from legacy raw
        # normalized={} even when checksum idempotency skips every source delivery.
        conn.execute("UPDATE mart.firm_month SET has_rejected_newer_delivery=false")
    assert ingest(tmp_path)["skipped"] == 2
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM raw.records").fetchone()["n"] == 2
        assert (
            conn.execute(
                "SELECT normalized FROM raw.records WHERE id=%s", (malformed["id"],)
            ).fetchone()["normalized"]
            == {}
        )

    client = TestClient(app)
    metric = client.get("/api/v1/firms/1/metrics").json()["items"][0]
    assert metric["has_rejected_newer_delivery"] is True
    assert metric["source_record_id"] == original["source_record_id"]
    assert Decimal(metric["revenue_eur"]) == Decimal("30")
    summary = client.get("/api/v1/quality/summary").json()
    assert summary["fallback_rows"] == summary["rejected_rows"] == 1
    issues = client.get("/api/v1/quality/issues?firm_id=1&severity=error").json()
    assert issues["total"] == 1
    issue = issues["items"][0]
    assert issue["filename"] == filename and issue["code"] == "column_count"
    assert issue["normalized"] == {}
    assert issue["identified_firm_id"] == "1"
    assert issue["identified_month"] == str(MONTH)
    lineage = client.get("/api/v1/lineage/1/2025-10").json()
    assert lineage["total"] == 2
    assert lineage["selected_record_id"] == original["source_record_id"]
    correction = lineage["versions"][0]
    assert correction["id"] == malformed["id"] and correction["filename"] == filename
    assert correction["status"] == "rejected" and correction["normalized"] == {}
    assert correction["identified_firm_id"] == "1"
    assert correction["identified_month"] == str(MONTH)


@pytest.mark.integration
def test_recovered_identity_validates_master_calendar_and_delivery_bounds(database, tmp_path):
    from fastapi.testclient import TestClient

    from pulse.db import connect
    from pulse.ingest import ingest
    from pulse_api.app import app

    cases = [
        ({"asesoria_id": " \t001 ", "mes": "\t2025-10 \n"}, "extra", "1", "2025-10-01"),
        ({"mes": "2020-05"}, "extra", "1", "2020-05-01"),
        ({}, "id_only", "1", None),
        ({"asesoria_id": "999"}, "extra", None, None),
        ({"asesoria_id": "0"}, "extra", None, None),
        ({"asesoria_id": "-1"}, "extra", None, None),
        ({"asesoria_id": "1.0"}, "extra", None, None),
        ({"asesoria_id": "x"}, "extra", None, None),
        ({"asesoria_id": ""}, "extra", None, None),
        ({"asesoria_id": "9" * 100}, "extra", None, None),
        ({"mes": ""}, "extra", "1", None),
        ({"mes": "2025-13"}, "extra", "1", None),
        ({"mes": "2025-1"}, "extra", "1", None),
        ({"mes": "0000-01"}, "extra", "1", None),
        ({"mes": "2025-12"}, "extra", "1", None),
        ({"mes": "2020-04"}, "extra", "1", None),
        ({"mes": "2025-10-01"}, "extra", "1", None),
    ]
    content = delivery(version=2)
    for change, shape, _, _ in cases:
        content += delivery(change, version=2, shape=shape).split(b"\r\n", 1)[1]
    drops = source(tmp_path)
    (drops / "metricas_2025-11.csv").write_bytes(content)
    ingest(tmp_path)
    with connect() as conn:
        records = conn.execute(
            """SELECT r.normalized,r.status,k.asesoria_id,k.mes FROM raw.records r
            JOIN raw.record_identity k ON k.record_id=r.id ORDER BY r.line_number"""
        ).fetchall()
        assert len(records) == len(cases)
        for record, (_, _, firm_id, month) in zip(records, cases, strict=True):
            assert record["status"] == "rejected" and record["normalized"] == {}
            assert (record["asesoria_id"], record["mes"]) == (firm_id, month)
        assert conn.execute("SELECT count(*) AS n FROM mart.firm_month").fetchone()["n"] == 0
    client = TestClient(app)
    issues = client.get("/api/v1/quality/issues?firm_id=1").json()
    assert issues["total"] == sum(firm == "1" for _, _, firm, _ in cases)
    assert all(item["identified_firm_id"] == "1" for item in issues["items"])
    lineage = client.get("/api/v1/lineage/1/2025-10").json()
    assert lineage["total"] == 1 and lineage["selected_record_id"] is None
    assert client.get("/api/v1/lineage/999/2025-10").status_code == 404
    assert client.get("/api/v1/lineage/1/2025-12").status_code == 404
