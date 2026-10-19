"""Exercise the investigation against PostgreSQL, archived CSVs, and the real API.

Revenue expectations are literal Decimal values, independent of the ingestion
parser. Every test reaches the existing guarded ``database`` fixture before it
can change database state. No external API or model service is used.
"""

import importlib
from datetime import date
from decimal import Decimal
from http.client import IncompleteRead

import pytest
from fastapi.testclient import TestClient
from test_ingestion import source
from test_parsing import make_csv

from pulse.db import connect
from pulse.ingest import ingest
from pulse_api.app import app

pytestmark = pytest.mark.integration

MONTH = date(2025, 10, 1)
CHECKS = {
    "archive_checksum",
    "source_payload",
    "revenue_normalization",
    "winner_selection",
    "mart_revenue",
    "api_revenue",
}


@pytest.fixture
def investigation(database):
    # Import after the database guard so an unconfigured developer machine can
    # collect/skip this suite even while the service is being implemented.
    return importlib.import_module("pulse_api.investigation")


@pytest.fixture
def fetch_api(database):
    with TestClient(app) as client:

        def fetch(firm_id, month):
            assert isinstance(firm_id, int)
            assert isinstance(month, str) and len(month) == 7
            date.fromisoformat(month + "-01")
            response = client.get(
                f"/api/v1/firms/{firm_id}/metrics", params={"start": month, "end": month}
            )
            return {
                "publication_id": response.headers.get("X-Pulse-Publication-ID"),
                "status_code": response.status_code,
                "body": response.json(),
            }

        yield fetch


@pytest.fixture
def published(database, tmp_path, fetch_api):
    drops = source(tmp_path)
    (drops / "metricas_2025-10.csv").write_bytes(make_csv([{}]))
    ingest(tmp_path)
    response = fetch_api(1, "2025-10")
    assert response["status_code"] == 200
    assert response["publication_id"], "The real metrics endpoint must expose its publication"
    return drops, record_id("metricas_2025-10.csv"), response["publication_id"]


def record_id(filename):
    with connect() as conn:
        return conn.execute(
            """SELECT r.id FROM raw.records r
            JOIN raw.deliveries d ON d.id=r.delivery_id
            WHERE d.filename=%s ORDER BY r.line_number LIMIT 1""",
            (filename,),
        ).fetchone()["id"]


def findings(report):
    assert report["status"] in {"consistent", "discrepancy", "inconclusive"}
    assert isinstance(report["findings"], list)
    result = {}
    for finding in report["findings"]:
        assert finding["status"] in {"match", "mismatch", "unavailable"}
        result.setdefault(finding["code"], set()).add(finding["status"])
    # Archive/row checks repeat per delivery/record. Never let a later matching
    # row hide a discrepancy in an earlier finding with the same code.
    return {
        code: next(status for status in ("mismatch", "unavailable", "match") if status in states)
        for code, states in result.items()
    }


def money(evidence, field, expected):
    value = evidence[field]
    assert isinstance(value, (Decimal, str)), f"{field} must retain decimal precision"
    assert Decimal(value) == Decimal(expected)


def assert_consistent(report, selected_id, publication_id, revenue, version_count):
    checks = findings(report)
    assert report["status"] == "consistent", [
        finding for finding in report["findings"] if finding["status"] != "match"
    ]
    assert {code: checks.get(code) for code in CHECKS} == dict.fromkeys(CHECKS, "match")
    assert "mismatch" not in checks.values()
    evidence = report["evidence"]
    assert str(evidence["publication_id"]) == publication_id
    assert evidence["selected_record_id"] == selected_id
    assert evidence["expected_record_id"] == selected_id
    for field in ("original_revenue_eur", "published_revenue_eur", "api_revenue_eur"):
        money(evidence, field, revenue)
    versions = evidence["versions"]
    assert versions["total"] == version_count
    assert len(versions["items"]) == version_count
    assert versions["truncated"] is False


@pytest.mark.parametrize(
    ("version", "filename", "month", "overrides", "expected"),
    [
        (1, "metricas_2025-10.csv", MONTH, {"facturacion_asesoria_eur": "120.25"}, "140.25"),
        (
            2,
            "metricas_2025-11.csv",
            MONTH,
            {"facturacion_asesoria_eur": "120,25", "facturacion_otros_eur": ""},
            "140.25",
        ),
        (
            2,
            "metricas_2025-11.csv",
            date(2025, 11, 1),
            {
                "mes": "2025-11",
                "facturacion_asesoria_eur": "120,25",
                "facturacion_otros_eur": "0,01",
            },
            "140.26",
        ),
        (
            1,
            "metricas_2025-10.csv",
            MONTH,
            {
                "facturacion_asesoria_eur": "0",
                "facturacion_gestion_eur": "0",
                "facturacion_consultoria_eur": "0",
            },
            "0",
        ),
    ],
    ids=["legacy-decimals", "new-schema-legacy-blank", "new-category-cent", "zero-revenue"],
)
def test_original_revenue_matches_real_api(
    investigation, tmp_path, fetch_api, version, filename, month, overrides, expected
):
    drops = source(tmp_path)
    (drops / filename).write_bytes(make_csv([overrides], version))
    ingest(tmp_path)
    api = fetch_api(1, month.strftime("%Y-%m"))
    assert api["status_code"] == 200
    assert api["publication_id"]
    report = investigation.investigate(1, month, fetch_api)
    assert_consistent(report, record_id(filename), api["publication_id"], expected, 1)


def test_corrections_choose_delivery_order_not_arrival_order(
    investigation, published, tmp_path, fetch_api
):
    drops, _, original_publication = published
    newest = "metricas_2025-11__002.csv"
    (drops / newest).write_bytes(
        make_csv([{"facturacion_asesoria_eur": "125,25", "facturacion_otros_eur": ""}], 2)
    )
    ingest(tmp_path)
    # Arriving last does not make a lower revision the winning correction.
    (drops / "metricas_2025-11__001.csv").write_bytes(
        make_csv([{"facturacion_asesoria_eur": "99,99", "facturacion_otros_eur": ""}], 2)
    )
    ingest(tmp_path)
    current = fetch_api(1, "2025-10")["publication_id"]
    assert current != original_publication
    report = investigation.investigate(1, MONTH, fetch_api)
    assert_consistent(report, record_id(newest), current, "145.25", 3)
    with connect() as conn:
        memberships = conn.execute(
            """SELECT p.id,count(pd.delivery_id) AS deliveries FROM ops.publications p
            JOIN ops.publication_deliveries pd ON pd.publication_id=p.id
            GROUP BY p.id ORDER BY deliveries"""
        ).fetchall()
    assert [row["deliveries"] for row in memberships] == [1, 2, 3]
    assert str(memberships[0]["id"]) == original_publication


def test_rejected_newer_delivery_preserves_valid_fallback(
    investigation, published, tmp_path, fetch_api
):
    drops, selected, _ = published
    (drops / "metricas_2025-11.csv").write_bytes(
        make_csv([{"satisfaccion_media": "99", "facturacion_asesoria_eur": "900"}], version=2)
    )
    ingest(tmp_path)
    api = fetch_api(1, "2025-10")
    assert api["body"]["items"][0]["has_rejected_newer_delivery"] is True
    report = investigation.investigate(1, MONTH, fetch_api)
    assert_consistent(report, selected, api["publication_id"], "30", 2)


@pytest.mark.parametrize(
    ("column", "code"), [("payload", "source_payload"), ("normalized", "revenue_normalization")]
)
def test_raw_tampering_is_detected_against_original_archive(
    investigation, published, fetch_api, column, code
):
    _, selected, _ = published
    with connect() as conn:
        # Column names are fixed by the parametrization, never external input.
        conn.execute(
            f"""UPDATE raw.records SET {column}=jsonb_set(
                {column}, '{{facturacion_asesoria_eur}}', '"999.00"'::jsonb)
                WHERE id=%s""",
            (selected,),
        )
    report = investigation.investigate(1, MONTH, fetch_api)
    checks = findings(report)
    assert report["status"] == "discrepancy"
    assert checks[code] == "mismatch"
    assert checks["archive_checksum"] == "match"
    money(report["evidence"], "original_revenue_eur", "30")
    money(report["evidence"], "published_revenue_eur", "30")


def test_parser_error_propagated_to_mart_and_api_is_still_detected(
    investigation, tmp_path, fetch_api, monkeypatch
):
    ingestion = importlib.import_module("pulse.ingest")
    original_parser = ingestion.parse_drop

    def broken_parser(*args, **kwargs):
        version, rows = original_parser(*args, **kwargs)
        rows[0].values["facturacion_asesoria_eur"] = Decimal("999")
        return version, rows

    drops = source(tmp_path)
    (drops / "metricas_2025-10.csv").write_bytes(make_csv([{}]))
    monkeypatch.setattr(ingestion, "parse_drop", broken_parser)
    ingest(tmp_path)
    report = investigation.investigate(1, MONTH, fetch_api)
    checks = findings(report)
    assert report["status"] == "discrepancy"
    assert checks["archive_checksum"] == checks["source_payload"] == "match"
    assert checks["revenue_normalization"] == "mismatch"
    money(report["evidence"], "original_revenue_eur", "30")
    money(report["evidence"], "published_revenue_eur", "1019")
    money(report["evidence"], "api_revenue_eur", "1019")


def test_investigation_does_not_reuse_production_csv_parser(
    investigation, published, fetch_api, monkeypatch
):
    parsing = importlib.import_module("pulse.parsing")
    parsers = (parsing.parse_drop, parsing.read_csv)

    def forbidden(*args, **kwargs):
        pytest.fail("Independent investigation must not call the production CSV parser")

    # Catch both module-qualified calls and aliases imported into the service.
    for name, value in list(vars(investigation).items()):
        if any(value is parser for parser in parsers):
            monkeypatch.setattr(investigation, name, forbidden)
    monkeypatch.setattr(parsing, "parse_drop", forbidden)
    monkeypatch.setattr(parsing, "read_csv", forbidden)
    _, selected, publication = published
    report = investigation.investigate(1, MONTH, fetch_api)
    assert_consistent(report, selected, publication, "30", 1)


def test_wrong_selected_record_is_detected_even_when_revenue_matches(
    investigation, published, tmp_path, fetch_api
):
    drops, older, _ = published
    correction = "metricas_2025-10__001.csv"
    (drops / correction).write_bytes(make_csv([{}]))
    ingest(tmp_path)
    expected = record_id(correction)
    with connect() as conn:
        conn.execute(
            "UPDATE mart.firm_month SET source_record_id=%s WHERE asesoria_id=1 AND mes=%s",
            (older, MONTH),
        )
    report = investigation.investigate(1, MONTH, fetch_api)
    assert report["status"] == "discrepancy"
    assert findings(report)["winner_selection"] == "mismatch"
    assert report["evidence"]["selected_record_id"] == older
    assert report["evidence"]["expected_record_id"] == expected
    money(report["evidence"], "original_revenue_eur", "30")
    money(report["evidence"], "published_revenue_eur", "30")


def test_mart_tampering_is_not_misattributed_to_api_serialization(
    investigation, published, fetch_api
):
    with connect() as conn:
        conn.execute(
            """UPDATE mart.firm_month SET facturacion_asesoria_eur=11
            WHERE asesoria_id=1 AND mes=%s""",
            (MONTH,),
        )
    report = investigation.investigate(1, MONTH, fetch_api)
    checks = findings(report)
    assert report["status"] == "discrepancy"
    assert checks["mart_revenue"] == "mismatch"
    assert checks["api_revenue"] == "match"
    assert checks["revenue_normalization"] == "match"
    money(report["evidence"], "original_revenue_eur", "30")
    money(report["evidence"], "published_revenue_eur", "31")
    money(report["evidence"], "api_revenue_eur", "31")


def test_api_revenue_serialization_tampering_is_detected(investigation, published, fetch_api):
    calls = []

    def altered_api(firm_id, month):
        calls.append((firm_id, month))
        response = fetch_api(firm_id, month)
        response["body"]["items"][0]["revenue_eur"] = "30.01"
        return response

    report = investigation.investigate(1, MONTH, altered_api)
    checks = findings(report)
    assert report["status"] == "discrepancy"
    assert checks["api_revenue"] == "mismatch"
    assert checks["mart_revenue"] == "match"
    money(report["evidence"], "original_revenue_eur", "30")
    money(report["evidence"], "api_revenue_eur", "30.01")
    assert calls == [(1, "2025-10")], "A stable publication discrepancy needs no retry"


def test_archive_checksum_tampering_is_detected(investigation, published, fetch_api):
    with connect() as conn:
        # Preserve a parseable CSV while invalidating its stored checksum.
        conn.execute(
            "UPDATE raw.deliveries SET content=%s WHERE filename=%s",
            (make_csv([{"facturacion_asesoria_eur": "99"}]), "metricas_2025-10.csv"),
        )
    report = investigation.investigate(1, MONTH, fetch_api)
    assert report["status"] == "discrepancy"
    assert findings(report)["archive_checksum"] == "mismatch"


def test_stored_line_outside_valid_archive_is_a_source_payload_mismatch(
    investigation, published, fetch_api
):
    _, selected, publication = published
    with connect() as conn:
        archived = conn.execute(
            """SELECT d.content,r.line_number FROM raw.records r
            JOIN raw.deliveries d ON d.id=r.delivery_id WHERE r.id=%s""",
            (selected,),
        ).fetchone()
        assert len(bytes(archived["content"]).splitlines()) == 2
        assert archived["line_number"] == 2
        conn.execute("UPDATE raw.records SET line_number=99 WHERE id=%s", (selected,))

    report = investigation.investigate(1, MONTH, fetch_api)
    checks = findings(report)
    assert report["status"] == "discrepancy"
    assert checks["archive_checksum"] == "match"
    assert checks["source_payload"] == "mismatch"
    payload_findings = [item for item in report["findings"] if item["code"] == "source_payload"]
    assert payload_findings
    assert all(item["status"] == "mismatch" for item in payload_findings)
    assert report["evidence"]["publication_id"] == publication
    assert report["evidence"]["selected_record_id"] == selected


@pytest.mark.parametrize(
    "publication_id", [None, "00000000-0000-0000-0000-000000000001"], ids=["missing", "different"]
)
def test_unverifiable_api_publication_is_inconclusive_after_bounded_retry(
    investigation, published, fetch_api, publication_id
):
    calls = []

    def changed_api(firm_id, month):
        calls.append((firm_id, month))
        response = fetch_api(firm_id, month)
        response["publication_id"] = publication_id
        response["body"]["items"][0]["revenue_eur"] = "9999"
        return response

    report = investigation.investigate(1, MONTH, changed_api)
    checks = findings(report)
    assert report["status"] == "inconclusive"
    assert checks["publication_changed"] == "unavailable"
    assert checks.get("api_revenue") != "mismatch", "Different snapshots cannot prove API drift"
    assert calls == [(1, "2025-10"), (1, "2025-10")]


def test_publication_race_retries_with_fresh_database_evidence(
    investigation, published, tmp_path, fetch_api
):
    drops, original_record, original_publication = published
    calls = []
    correction = "metricas_2025-10__001.csv"

    def publish_before_response(firm_id, month):
        calls.append((firm_id, month))
        if len(calls) == 1:
            (drops / correction).write_bytes(make_csv([{"facturacion_asesoria_eur": "25"}]))
            ingest(tmp_path)
        return fetch_api(firm_id, month)

    report = investigation.investigate(1, MONTH, publish_before_response)
    current = fetch_api(1, "2025-10")["publication_id"]
    selected = record_id(correction)
    assert current != original_publication
    assert selected != original_record
    assert calls == [(1, "2025-10"), (1, "2025-10")]
    assert_consistent(report, selected, current, "45", 2)


def test_failed_publication_does_not_blame_stale_mart_for_new_raw_rows(
    investigation, published, tmp_path, fetch_api, monkeypatch
):
    drops, selected, publication = published
    ingestion = importlib.import_module("pulse.ingest")
    real_publish = ingestion.publish
    correction = "metricas_2025-10__001.csv"
    (drops / correction).write_bytes(make_csv([{"facturacion_asesoria_eur": "999"}]))

    def fail_after_publish(conn, run_id):
        # Exercise rollback of the mart AND the new publication/membership rows.
        real_publish(conn, run_id)
        raise RuntimeError("Injected failure after publication writes")

    with monkeypatch.context() as patch:
        patch.setattr(ingestion, "publish", fail_after_publish)
        with pytest.raises(RuntimeError, match="Injected failure"):
            ingest(tmp_path)

    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM raw.deliveries").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM ops.publications").fetchone()["n"] == 1
        assert (
            conn.execute("SELECT count(*) AS n FROM ops.publication_deliveries").fetchone()["n"]
            == 1
        )
        assert (
            conn.execute("SELECT count(*) AS n FROM ops.runs WHERE status='failed'").fetchone()["n"]
            == 1
        )
    assert fetch_api(1, "2025-10")["publication_id"] == publication
    report = investigation.investigate(1, MONTH, fetch_api)
    checks = findings(report)
    assert report["status"] in {"consistent", "inconclusive"}
    assert "mismatch" not in checks.values()
    assert checks["winner_selection"] == checks["mart_revenue"] == checks["api_revenue"] == "match"
    evidence = report["evidence"]
    assert evidence["publication_id"] == publication
    assert evidence["selected_record_id"] == evidence["expected_record_id"] == selected
    for field in ("original_revenue_eur", "published_revenue_eur", "api_revenue_eur"):
        money(evidence, field, "30")
    assert evidence["versions"]["total"] == 2
    assert len(evidence["versions"]["items"]) == 2
    assert evidence["versions"]["truncated"] is False

    # A successful retry must now include the archived correction in its snapshot.
    assert ingest(tmp_path)["skipped"] == 2
    current = fetch_api(1, "2025-10")["publication_id"]
    assert current != publication
    report = investigation.investigate(1, MONTH, fetch_api)
    assert_consistent(report, record_id(correction), current, "1019", 2)


@pytest.mark.parametrize("with_rejected_row", [False, True], ids=["no-source", "only-rejected"])
def test_missing_accepted_record_is_inconclusive(
    investigation, tmp_path, fetch_api, with_rejected_row
):
    drops = source(tmp_path)
    row = {"satisfaccion_cliente": "99"} if with_rejected_row else {"mes": "2025-09"}
    (drops / "metricas_2025-10.csv").write_bytes(make_csv([row]))
    ingest(tmp_path)
    report = investigation.investigate(1, MONTH, fetch_api)
    checks = findings(report)
    assert report["status"] == "inconclusive"
    assert "unavailable" in checks.values()
    assert "mismatch" not in checks.values()
    evidence = report["evidence"]
    assert evidence["publication_id"] is not None
    assert evidence["selected_record_id"] is None
    assert evidence["expected_record_id"] is None
    assert evidence["original_revenue_eur"] is None
    assert evidence["published_revenue_eur"] is None
    assert evidence["api_revenue_eur"] is None
    assert evidence["versions"]["total"] == int(with_rejected_row)
    assert evidence["versions"]["truncated"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("asesoria_id", 999),
        ("mes", "2025-09-01"),
        ("source_record_id", 999),
        ("facturacion_asesoria_eur", "0.00"),
        ("facturacion_gestion_eur", "0.00"),
        ("facturacion_consultoria_eur", "0.00"),
        ("facturacion_otros_eur", "0.00"),
    ],
    ids=[
        "wrong-firm",
        "wrong-month",
        "non-null-source",
        "asesoria",
        "gestion",
        "consultoria",
        "otros",
    ],
)
def test_missing_accepted_source_does_not_hide_malformed_api_item(
    investigation, tmp_path, fetch_api, field, value
):
    drops = source(tmp_path)
    (drops / "metricas_2025-10.csv").write_bytes(make_csv([{"satisfaccion_cliente": "99"}]))
    ingest(tmp_path)
    calls = []

    def altered_missing_api(firm_id, month):
        calls.append((firm_id, month))
        response = fetch_api(firm_id, month)
        assert response["status_code"] == 200
        assert response["publication_id"]
        assert len(response["body"]["items"]) == 1
        item = response["body"]["items"][0]
        assert item["asesoria_id"] == firm_id
        assert item["mes"] == str(MONTH)
        assert item["coverage_status"] == "missing"
        assert item["source_record_id"] is None
        assert item["revenue_eur"] is None
        for component in (
            "facturacion_asesoria_eur",
            "facturacion_gestion_eur",
            "facturacion_consultoria_eur",
            "facturacion_otros_eur",
        ):
            assert item[component] is None
        # Change one field at a time; a null total must not mask invalid identity
        # or non-null components (including zero) on a missing metric.
        item[field] = value
        return response

    report = investigation.investigate(1, MONTH, altered_missing_api)
    checks = findings(report)
    assert report["status"] == "discrepancy"
    assert checks["no_accepted_source"] == "unavailable"
    assert checks["api_revenue"] == "mismatch"
    evidence = report["evidence"]
    assert evidence["selected_record_id"] is None
    assert evidence["expected_record_id"] is None
    assert evidence["published_revenue_eur"] is None
    assert evidence["api_revenue_eur"] is None
    assert calls == [(1, "2025-10")]


def test_database_evidence_is_read_only_and_connection_closes_before_api(
    investigation, published, fetch_api, monkeypatch
):
    connections, read_only_states = [], []
    real_connect = investigation.connect

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        execute = conn.execute

        def inspect_read(query, *query_args, **query_kwargs):
            result = execute(query, *query_args, **query_kwargs)
            if isinstance(query, str) and query.lstrip().upper().startswith(("SELECT", "WITH")):
                state = execute("SHOW transaction_read_only").fetchone()["transaction_read_only"]
                read_only_states.append(state)
                assert state == "on", "Evidence must be collected in a read-only transaction"
            return result

        monkeypatch.setattr(conn, "execute", inspect_read)
        return conn

    def after_database_close(firm_id, month):
        assert connections, "Investigation must collect database evidence before calling HTTP"
        assert all(conn.closed for conn in connections), "Do not hold database sessions over HTTP"
        return fetch_api(firm_id, month)

    monkeypatch.setattr(investigation, "connect", tracked_connect)
    report = investigation.investigate(1, MONTH, after_database_close)
    _, selected, publication = published
    assert_consistent(report, selected, publication, "30", 1)
    assert read_only_states
    assert all(conn.closed for conn in connections)


def test_collected_archive_evidence_is_not_reread_after_http(investigation, published, fetch_api):
    def change_archive_after_snapshot(firm_id, month):
        with connect() as conn:
            conn.execute(
                "UPDATE raw.deliveries SET content=%s WHERE filename=%s",
                (make_csv([{"facturacion_asesoria_eur": "999"}]), "metricas_2025-10.csv"),
            )
        return fetch_api(firm_id, month)

    report = investigation.investigate(1, MONTH, change_archive_after_snapshot)
    _, selected, publication = published
    assert_consistent(report, selected, publication, "30", 1)
    # A subsequent investigation must observe the corruption, proving the first
    # report used captured evidence rather than simply ignoring the archive.
    fresh = investigation.investigate(1, MONTH, fetch_api)
    assert fresh["status"] == "discrepancy"
    assert findings(fresh)["archive_checksum"] == "mismatch"


@pytest.mark.parametrize("failure", ["http-503", "transport-error"])
def test_api_unavailability_is_not_a_revenue_discrepancy(
    investigation, published, fetch_api, failure
):
    calls = []

    def unavailable_api(firm_id, month):
        calls.append((firm_id, month))
        if failure == "transport-error":
            raise OSError("Injected local transport failure")
        response = fetch_api(firm_id, month)
        response["status_code"] = 503
        response["body"] = {"detail": "Injected unavailable response"}
        return response

    report = investigation.investigate(1, MONTH, unavailable_api)
    checks = findings(report)
    assert report["status"] == "inconclusive"
    assert checks["api_revenue"] == "unavailable"
    assert "mismatch" not in checks.values()
    assert report["evidence"]["api_revenue_eur"] is None
    assert calls == [(1, "2025-10")]


def test_incomplete_http_response_is_inconclusive_api_unavailable(investigation, published):
    calls = []

    def incomplete_api(firm_id, month):
        calls.append((firm_id, month))
        raise IncompleteRead(b'{"items":', 100)

    report = investigation.investigate(1, MONTH, incomplete_api)
    checks = findings(report)
    assert report["status"] == "inconclusive"
    assert checks["api_revenue"] == "unavailable"
    assert "mismatch" not in checks.values()
    assert report["evidence"]["api_revenue_eur"] is None
    _, selected, publication = published
    assert report["evidence"]["publication_id"] == publication
    assert report["evidence"]["selected_record_id"] == selected
    money(report["evidence"], "published_revenue_eur", "30")
    assert calls == [(1, "2025-10")]


def test_truncated_history_still_checks_selected_fallback_outside_window(
    investigation, published, tmp_path, fetch_api
):
    drops, selected, _ = published
    # The implementation bounds history at 50. Keep the only accepted record
    # older than that entire window so winner verification cannot use items[0].
    for revision in range(1, 52):
        (drops / f"metricas_2025-10__{revision:03d}.csv").write_bytes(
            make_csv([{"satisfaccion_cliente": "99"}])
        )
    ingest(tmp_path)
    report = investigation.investigate(1, MONTH, fetch_api)
    checks = findings(report)
    assert report["status"] == "inconclusive"
    assert "mismatch" not in checks.values()
    assert checks["winner_selection"] == checks["mart_revenue"] == "match"
    evidence = report["evidence"]
    versions = evidence["versions"]
    assert versions["total"] == 52
    assert 0 < len(versions["items"]) < versions["total"]
    assert versions["truncated"] is True
    assert selected not in {item["id"] for item in versions["items"]}
    assert evidence["selected_record_id"] == evidence["expected_record_id"] == selected
    money(evidence, "original_revenue_eur", "30")
