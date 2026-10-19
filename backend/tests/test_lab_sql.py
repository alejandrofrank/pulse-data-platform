import time

import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from test_ingestion import source
from test_parsing import make_csv

from pulse.db import connect
from pulse.ingest import ingest
from pulse_api.lab_api import app

pytestmark = pytest.mark.integration
HEADERS = {"X-Pulse-Lab": "playback-v1"}


@pytest.fixture
def sql_client(database, tmp_path, monkeypatch):
    drops = source(tmp_path)
    (drops / "metricas_2025-10.csv").write_bytes(make_csv([{}]))
    monkeypatch.setenv("LAB_DATABASE_URL", database)
    monkeypatch.setenv("PULSE_DATA_DIR", str(tmp_path))
    ingest(tmp_path, database_url=database)
    monkeypatch.setenv(
        "DATABASE_URL", make_conninfo(database, user="pulse_reader", password="pulse_reader_local")
    )
    with TestClient(app) as client:
        yield client


def run(client, query, **options):
    return client.post("/api/lab/sql", headers=HEADERS, json={"sql": query, **options})


def test_sql_queries_real_tables_ctes_and_preserves_exact_values(sql_client):
    result = run(
        sql_client,
        """WITH source AS (
        SELECT f.asesoria_id,f.mes,d.filename,r.payload
        FROM mart.firm_month f JOIN raw.records r ON r.id=f.source_record_id
        JOIN raw.deliveries d ON d.id=r.delivery_id)
        SELECT filename,payload->>'asesoria_id',9007199254740993::bigint AS exact,
        1234567890123456.78::numeric AS exact,NULL AS missing,
        row_number() OVER (ORDER BY mes) FROM source; -- a real SELECT""",
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["rows"] == [
        ["metricas_2025-10.csv", "1", "9007199254740993", "1234567890123456.78", None, "1"]
    ]
    assert [c["name"] for c in body["columns"]].count("exact") == 2
    assert body["role"] == "pulse_reader" and not body["truncated"]
    assert body["columns"][2]["type"] == "bigint"
    assert run(sql_client, "SELECT ';' AS semicolon, $$DELETE;$$ AS literal").status_code == 200


def test_sql_schema_plan_and_limits(sql_client, monkeypatch):
    schema = sql_client.get("/api/lab/sql/schema").json()
    tables = {(t["schema"], t["name"]): t for t in schema["tables"]}
    assert ("lab", "jobs") in tables and ("mart", "firm_month") in tables
    assert tables[("raw", "records")]["columns"][0]["primary_key"]
    plan = run(sql_client, "SELECT * FROM mart.firm_month", explain=True).json()
    assert plan["explain"] and "Scan" in str(plan["rows"])
    limited = run(sql_client, "SELECT generate_series(1,1000000000) AS n", limit=2).json()
    assert limited["rows"] == [["1"], ["2"]] and limited["truncated"]
    exact = run(sql_client, "SELECT generate_series(1,2) AS n", limit=2).json()
    assert not exact["truncated"]
    empty = run(sql_client, "SELECT id FROM core.firms WHERE false").json()
    assert empty["columns"][0]["name"] == "id" and empty["rows"] == []
    monkeypatch.setattr("pulse_api.lab_sql.MAX_BYTES", 100)
    large = run(sql_client, "SELECT repeat('x',101)").json()
    assert large["truncation_reason"] == "byte_limit" and large["rows"] == []


def test_sql_rejects_commands_and_multiple_statements_without_mutation(sql_client, database):
    commands = [
        "DELETE FROM mart.firm_month",
        "SELECT 1; DELETE FROM mart.firm_month",
        "SELECT 1; COMMIT; UPDATE core.firms SET nombre='changed'",
        "WITH changed AS (DELETE FROM mart.firm_month RETURNING *) SELECT * FROM changed",
        "SELECT * FROM mart.firm_month FOR UPDATE",
        "SET ROLE lab_owner",
        "COPY core.firms TO PROGRAM 'echo unsafe'",
        "SELECT set_config('role','pulse',true)",
        "SELECT set_config('transaction_read_only','off',true)",
    ]
    for command in commands:
        result = run(sql_client, command)
        assert result.status_code == 400, (command, result.text)
    with connect(database) as conn:
        assert conn.execute("SELECT count(*) AS n FROM mart.firm_month").fetchone()["n"] == 1
        assert conn.execute("SELECT nombre FROM core.firms").fetchone()["nombre"] == "Example"
    assert run(sql_client, "SELEC broken").json()["detail"]["sqlstate"] == "42601"
    assert run(sql_client, "SELECT count(*) FROM mart.firm_month").json()["rows"] == [["1"]]


def test_sql_request_and_connection_guards(sql_client, database, monkeypatch):
    assert sql_client.post("/api/lab/sql", json={"sql": "SELECT 1"}).status_code == 403
    assert (
        sql_client.post(
            "/api/lab/sql",
            headers={**HEADERS, "Origin": "https://example.org"},
            json={"sql": "SELECT 1"},
        ).status_code
        == 403
    )
    assert run(sql_client, " ").status_code == 422
    assert run(sql_client, "SELECT 1", limit=1001).status_code == 422
    monkeypatch.setenv("DATABASE_URL", database)
    assert run(sql_client, "SELECT 1").status_code == 503


def test_sql_timeout_releases_connection_and_query_slot(sql_client):
    start = time.monotonic()
    result = run(sql_client, "SELECT set_config('statement_timeout','0',true),pg_sleep(10)")
    assert result.status_code == 408, result.text
    assert time.monotonic() - start < 8
    assert run(sql_client, "SELECT 42").json()["rows"] == [["42"]]
