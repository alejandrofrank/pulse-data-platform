"""Pure/unit checks run without PostgreSQL; integration checks use only database.

Run the integration group serially with the repository's disposable *_test fixture.
The path bootstrap supports this package before the parent change registers it.
"""

import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "04_agents" / "src"))

from pulse_agents import (  # noqa: E402
    Metric,
    Operation,
    QueryPlan,
    analytics,
    query_plan_json_schema,
)


def plan(**changes):
    return QueryPlan.model_validate(
        {
            "action": "query",
            "metric": "revenue_eur",
            "operation": "total",
            "start": "2025-01",
            "end": "2025-04",
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"sql": "SELECT 1"},
        {"action": "delete"},
        {"metric": "profit"},
        {"operation": "average"},
        {"start": "2025-00"},
        {"end": "2025-13"},
        {"start": "0000-01"},
        {"start": "2025-1"},
        {"start": "2025-01-01"},
        {"start": " 2025-01"},
        {"start": date(2025, 1, 1)},
        {"start": 202501},
        {"start": "2025-05"},
        {"start": "2015-04", "end": "2025-04"},
        {"firm_id": 0},
        {"firm_id": -1},
        {"firm_id": True},
        {"firm_id": "1"},
        {"firm_id": 2**31},
        {"firm_name": " "},
        {"firm_name": "a" * 201},
        {"firm_name": "A\x00B"},
        {"firm_name": 12},
        {"months_declining": 0},
        {"months_declining": 13},
        {"months_declining": True},
        {"months_declining": "3"},
        {"months_declining": 3.0},
        {"clarification": "a" * 501},
        {"clarification": None},
    ],
)
def test_strict_plan_validation(changes):
    with pytest.raises(ValidationError):
        plan(**changes)


def test_calendar_boundaries_defaults_and_schema():
    assert plan(start="2015-05", end="2025-04").months_declining == 3
    assert plan(start="9999-12", end="9999-12").end == "9999-12"
    assert plan(start="0001-01", end="0001-01").start == "0001-01"
    assert plan(firm_name="  Example  ").firm_name == "Example"
    minimal = QueryPlan(action="clarify")
    assert minimal.clarification == "" and minimal.metric is None
    with pytest.raises(ValidationError):
        minimal.start = "2025-01"
    schema = query_plan_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(QueryPlan.model_fields)
    assert all("default" not in field for field in schema["properties"].values())
    assert {"type": "null"} in schema["properties"]["metric"]["anyOf"]
    assert set(schema["$defs"]["Metric"]["enum"]) == {metric.value for metric in Metric}
    assert set(schema["$defs"]["Operation"]["enum"]) == {op.value for op in Operation}
    json.dumps(schema)


@pytest.mark.parametrize("operation", list(Operation))
def test_compiler_is_pure_bounded_and_parameterized(operation, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Compiling must not connect to a database")

    monkeypatch.setattr(analytics.psycopg, "connect", forbidden)
    metric = "clientes_activos" if operation == Operation.DECLINE else "revenue_eur"
    query = analytics.compile_plan(plan(operation=operation, metric=metric), (7, 2, 7))
    assert query.parameters[:2] == (date(2025, 1, 1), date(2025, 4, 1))
    assert query.parameters[2:4] == ([2, 7], [2, 7])
    assert "2025-01" not in query.sql
    assert query.sql.count("%s") == len(query.parameters)
    assert query.coverage_sql.count("%s") == len(query.coverage_parameters)
    assert "LIMIT %s" in query.sql and "LIMIT %s" in query.coverage_sql
    assert "coalesce" not in query.sql.lower()
    assert "published_run_id=(SELECT id FROM publication)" in query.sql
    assert query.row_limit <= 120
    assert query.coverage_parameters[-1] == 120
    if operation in (Operation.DECLINE, Operation.RANKING):
        assert query.parameters[-1] == 101 and query.row_limit == 100


@pytest.mark.parametrize("metric", list(Metric))
def test_catalogue_is_complete_and_uses_fixed_expressions(metric):
    query = analytics.compile_plan(plan(metric=metric, start="2025-04"))
    assert analytics.METRIC_CATALOGUE[metric].expression in query.sql
    assert analytics.METRIC_CATALOGUE[metric].definition
    with pytest.raises(TypeError):
        analytics.METRIC_CATALOGUE[metric] = None


@pytest.mark.parametrize("scope", [[], [1], (True,), (0,), (-1,), ("1",), (2**31,), (1,) * 10001])
def test_invalid_scope_rejected(scope):
    with pytest.raises(ValueError):
        analytics.compile_plan(plan(), scope)


@pytest.mark.parametrize("scope,firm_id", [((), None), ((2,), 1)])
def test_scope_denies_before_any_connection(scope, firm_id, monkeypatch):
    monkeypatch.setattr(analytics.psycopg, "connect", lambda *a, **kw: pytest.fail("No DB access"))
    with pytest.raises(PermissionError):
        analytics.compile_plan(plan(firm_id=firm_id), scope)
    result = analytics.execute_plan(plan(firm_id=firm_id), scope)
    assert result["status"] == "unsupported" and result["code"] == "scope_denied"
    assert result["evidence"]["rows"] == []


def test_none_scope_is_distinct_from_bounded_scope():
    assert analytics.compile_plan(plan()).parameters[2:4] == (None, None)
    assert analytics.compile_plan(plan(), (1,)).parameters[2:4] == ([1], [1])


@pytest.mark.parametrize(
    "changes,exception",
    [
        ({"start": None}, analytics.PlanClarification),
        ({"end": None}, analytics.PlanClarification),
        ({"metric": None}, analytics.PlanClarification),
        ({"operation": None}, analytics.PlanClarification),
        ({"action": "clarify"}, analytics.PlanClarification),
        ({"action": "investigate"}, analytics.PlanUnsupported),
        ({"action": "unsupported"}, analytics.PlanUnsupported),
        ({"metric": "clientes_activos"}, analytics.PlanClarification),
        ({"metric": "clientes_activos", "operation": "ranking"}, analytics.PlanClarification),
        ({"operation": "decline"}, analytics.PlanUnsupported),
        (
            {"operation": "decline", "metric": "clientes_activos", "start": "2025-02"},
            analytics.PlanClarification,
        ),
        ({"firm_name": "Example"}, analytics.PlanClarification),
    ],
)
def test_no_silent_business_assumptions(changes, exception):
    with pytest.raises(exception):
        analytics.compile_plan(plan(**changes))


def test_three_declines_require_four_observations():
    query = analytics.compile_plan(plan(metric="clientes_activos", operation="decline"))
    assert query.parameters[-6:] == (3, 3, 2, 3, 3, 101)
    assert "previous_month+interval '1 month'" in query.sql
    assert "previous_value IS NOT NULL" in query.sql
    assert "value<previous_value" in query.sql
    assert "PARTITION BY firm_id" in query.sql


def test_model_copy_cannot_bypass_validation():
    with pytest.warns(UserWarning, match="Pydantic serializer"), pytest.raises(ValidationError):
        analytics.compile_plan(
            plan().model_copy(update={"metric": "x; DROP TABLE mart.firm_month"})
        )
    with pytest.raises(ValidationError):
        analytics.execute_plan(plan().model_copy(update={"start": "2000-01"}))


@pytest.mark.parametrize("exact", [True, False])
def test_names_are_literal_scoped_and_parameterized(exact):
    name = "O'Brien %_!\\'; DROP TABLE core.firms;--"
    sql, parameters = analytics.compile_firm_lookup(
        plan(firm_name=name, firm_id=7),
        (7, 8),
        exact=exact,
    )
    escaped = name.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    assert parameters == ([7, 8], [7, 8], 7, 7, escaped if exact else "%" + escaped + "%", 21)
    assert name not in sql and "DROP TABLE" not in sql
    assert "ILIKE %s ESCAPE '!'" in sql
    assert sql.count("%s") == len(parameters)


class FakeCursor:
    def __init__(self, rows, columns=None):
        self.rows = rows
        self.description = [
            SimpleNamespace(name=key) for key in (columns or (list(rows[0]) if rows else []))
        ]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConnection:
    """Cursor protocol double for execution behavior, never a SQL evaluator."""

    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def execute(self, sql, parameters=()):
        self.calls.append((sql, parameters))
        if sql.startswith(("SET TRANSACTION", "SELECT set_config")):
            return FakeCursor([])
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return FakeCursor(response)


def fake_reader(monkeypatch, *responses, writer=False, database_name="pulse"):
    conn = FakeConnection(
        [
            [{"database_name": database_name, "can_write": writer}],
            [{"id": "publication-1"}],
            *responses,
        ]
    )
    options = {}

    def connect(url, **kwargs):
        options.update(kwargs)
        return conn

    monkeypatch.setenv("DATABASE_URL", "postgresql://not-a-real-database")
    monkeypatch.setattr(analytics.psycopg, "connect", connect)
    return conn, options


def coverage(month=date(2025, 4, 1), expected=1, observed=1, reported=1):
    return {
        "month": month,
        "expected_firm_months": expected,
        "observed_firm_months": observed,
        "reported_firm_months": reported,
        "fallback_firm_months": 0,
    }


def test_dedicated_readonly_snapshot_exact_money_and_connection_cleanup(monkeypatch):
    amount = Decimal("999999999999999999999999.99")
    conn, options = fake_reader(monkeypatch, [{"value": amount}], [coverage()])
    result = analytics.execute_plan(plan(start="2025-04"), (1,))
    assert result["status"] == "answered"
    assert result["evidence"]["rows"][0]["value"] == str(amount)
    assert str(amount) in result["answer"]
    assert result["evidence"]["publication_id"] == "publication-1"
    assert result["evidence"]["coverage"]["complete"] is True
    assert result["evidence"]["parameters"][:2] == ["2025-04-01", "2025-04-01"]
    assert conn.calls[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert "default_transaction_read_only=on" in options["options"]
    assert "repeatable\\ read" in options["options"]
    assert "lock_timeout=1000" in options["options"]
    assert options["connect_timeout"] == 5
    assert conn.closed
    assert "999999999999999999999999.99" in json.dumps(result)


def test_writer_is_rejected_except_disposable_fixture(monkeypatch):
    conn, _ = fake_reader(monkeypatch, writer=True)
    result = analytics.execute_plan(plan())
    assert result["code"] == "reader_required" and conn.closed
    assert analytics.PUBLICATION_SQL not in [sql for sql, _ in conn.calls]
    fake_reader(
        monkeypatch,
        [{"value": Decimal("0")}],
        [coverage()],
        writer=True,
        database_name="pulse_test",
    )
    assert analytics.execute_plan(plan(start="2025-04"))["status"] == "answered"


@pytest.mark.parametrize(
    "matches,code",
    [
        ([], "firm_not_found"),
        (
            [{"firm_id": 1, "firm_name": "Example"}, {"firm_id": 2, "firm_name": "Example"}],
            "ambiguous_firm",
        ),
        ([{"firm_id": i, "firm_name": f"Example {i}"} for i in range(1, 22)], "ambiguous_firm"),
    ],
)
def test_name_resolution_never_picks_ambiguous_match(monkeypatch, matches, code):
    responses = [matches] if matches else [[], []]
    conn, _ = fake_reader(monkeypatch, *responses)
    result = analytics.execute_plan(plan(firm_name="Example"), tuple(range(1, 22)))
    assert result["status"] == "clarification" and result["code"] == code
    assert len(result["candidates"]) <= 20
    assert result["evidence"]["truncated"] == (len(matches) > 20)
    assert not any("WITH publication" in sql for sql, _ in conn.calls)


@pytest.mark.parametrize("exact_match", [True, False])
def test_unique_name_resolution_retains_authorization(monkeypatch, exact_match):
    matches = [{"firm_id": 2, "firm_name": "Example"}]
    responses = [matches] if exact_match else [[], matches]
    conn, _ = fake_reader(monkeypatch, *responses, [{"value": Decimal("12.10")}], [coverage()])
    result = analytics.execute_plan(plan(start="2025-04", firm_name="Example"), (2, 7))
    assert result["status"] == "answered"
    assert result["evidence"]["parameters"][2:6] == [[2, 7], [2, 7], 2, 2]
    lookups = [sql for sql, _ in conn.calls if "ILIKE" in sql]
    assert len(lookups) == (1 if exact_match else 2)


def test_missing_metric_is_unknown_not_zero(monkeypatch):
    fake_reader(monkeypatch, [{"value": None}], [coverage(observed=0)])
    result = analytics.execute_plan(plan(start="2025-04", metric="horas_trabajadas"))
    assert result["status"] == "answered"
    assert result["evidence"]["rows"][0]["value"] is None
    assert result["evidence"]["coverage"]["null_metric_firm_months"] == 1
    assert result["evidence"]["coverage"]["complete"] is False
    assert "absence is not zero" in result["answer"]


def test_ranking_truncation_does_not_truncate_scope_coverage(monkeypatch):
    rows = [
        {"firm_id": i, "firm_name": f"Firm {i}", "value": Decimal("10.01")} for i in range(1, 102)
    ]
    fake_reader(monkeypatch, rows, [coverage(expected=500, observed=450, reported=470)])
    result = analytics.execute_plan(plan(start="2025-04", operation="ranking"))
    assert len(result["evidence"]["rows"]) == 100
    assert result["evidence"]["truncated"] is True
    assert result["evidence"]["coverage"]["observed_firm_months"] == 450
    assert "450/500" in result["answer"] and "truncated" in result["answer"]


def test_timeout_clears_partial_rows_and_never_exposes_connection_details(monkeypatch):
    conn, _ = fake_reader(
        monkeypatch,
        [{"value": Decimal("99.01")}],
        analytics.psycopg.errors.QueryCanceled("secret-DSN"),
    )
    result = analytics.execute_plan(plan())
    assert result["code"] == "query_timeout"
    assert result["evidence"]["rows"] == [] and result["evidence"]["coverage"] is None
    assert "secret-DSN" not in json.dumps(result)
    assert conn.closed


def test_deadline_is_shared_across_statements(monkeypatch):
    conn, _ = fake_reader(monkeypatch)
    ticks = iter([0, 0, 11])
    monkeypatch.setattr(analytics, "monotonic", lambda: next(ticks))
    result = analytics.execute_plan(plan())
    assert result["code"] == "query_timeout"
    assert analytics.PUBLICATION_SQL not in [sql for sql, _ in conn.calls]
    assert conn.closed


def test_missing_configuration_and_incomplete_intent_never_connect(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(analytics.psycopg, "connect", lambda *a, **kw: pytest.fail("No DB access"))
    assert analytics.execute_plan(plan())["code"] == "database_unavailable"
    assert analytics.execute_plan(plan(start=None))["status"] == "clarification"
    assert analytics.execute_plan(plan(action="investigate"))["status"] == "unsupported"


def test_missing_publication_never_claims_zero(monkeypatch):
    conn, _ = fake_reader(monkeypatch)
    conn.responses = iter([[{"database_name": "pulse", "can_write": False}], []])
    result = analytics.execute_plan(plan())
    assert result["code"] == "no_publication" and result["status"] == "clarification"
    assert result["evidence"]["publication_id"] is None and result["evidence"]["rows"] == []
    assert conn.closed


@pytest.mark.parametrize(
    "error,code",
    [
        (analytics.psycopg.OperationalError("secret-DSN"), "database_unavailable"),
        (analytics.psycopg.errors.InsufficientPrivilege("secret-DSN"), "database_error"),
    ],
)
def test_database_errors_do_not_expose_driver_details(monkeypatch, error, code):
    conn, _ = fake_reader(monkeypatch, error)
    result = analytics.execute_plan(plan())
    assert result["status"] == "unsupported" and result["code"] == code
    assert "secret-DSN" not in json.dumps(result) and conn.closed


@pytest.fixture
def agent_publication(database, tmp_path):
    """Reuse the guarded database fixture; write synthetic inputs only in tmp_path."""
    from test_parsing import make_csv

    from pulse.ingest import ingest

    drops = tmp_path / "drops"
    drops.mkdir()
    (tmp_path / "asesorias.csv").write_text(
        "id,nombre,cif,provincia,ciudad,fecha_alta,num_empleados,especialidad\n"
        "1,Alpha,B12345671,Madrid,Madrid,2020-01-01,5,Fiscal\n"
        "2,Alpha East,B12345672,Madrid,Madrid,2020-01-01,5,Fiscal\n"
        "3,Alpha West,B12345673,Madrid,Madrid,2020-01-01,5,Fiscal\n"
        "4,Literal %_!,B12345674,Madrid,Madrid,2025-03-01,5,Fiscal\n",
        encoding="utf-8",
    )
    # Firm 1 strictly declines; firm 2 has a missing March; firm 3 has a plateau.
    for index, month in enumerate(("2025-01", "2025-02", "2025-03", "2025-04")):
        rows = []
        for firm, stocks in (
            (1, [100, 90, 80, 70]),
            (2, [100, 90, None, 70]),
            (3, [100, 90, 90, 70]),
            (4, [None, None, 20, 19]),
        ):
            if stocks[index] is None:
                continue
            rows.append(
                {
                    "asesoria_id": str(firm),
                    "mes": month,
                    "clientes_activos": str(stocks[index]),
                    "facturacion_asesoria_eur": "0.10",
                    "facturacion_gestion_eur": "0.20",
                    "facturacion_consultoria_eur": "0",
                    "horas_trabajadas": "" if firm == 1 else "1.25",
                }
            )
        (drops / f"metricas_{month}.csv").write_bytes(make_csv(rows))
    ingest(tmp_path)
    return database


@pytest.mark.integration
def test_database_series_totals_coverage_and_exact_revenue(agent_publication):
    total = analytics.execute_plan(plan(), (1,))
    assert total["status"] == "answered"
    assert Decimal(total["evidence"]["rows"][0]["value"]) == Decimal("1.20")
    assert total["evidence"]["publication_id"]
    assert total["evidence"]["coverage"]["complete"] is True
    series = analytics.execute_plan(plan(operation="series"))
    rows = series["evidence"]["rows"]
    assert [Decimal(row["value"]) for row in rows] == [Decimal("0.90")] * 3 + [Decimal("1.20")]
    assert [row["expected_firm_months"] for row in rows] == [3, 3, 4, 4]
    assert series["evidence"]["coverage"]["missing_firm_months"] == 1
    assert series["evidence"]["coverage"]["complete"] is False


@pytest.mark.integration
def test_database_decline_requires_contiguous_strict_stocks(agent_publication):
    result = analytics.execute_plan(plan(metric="clientes_activos", operation="decline"))
    rows = result["evidence"]["rows"]
    assert result["status"] == "answered"
    assert len(rows) == 1 and rows[0]["firm_id"] == 1
    assert rows[0]["start_month"] == "2025-01-01" and rows[0]["end_month"] == "2025-04-01"
    assert rows[0]["start_value"] == 100 and rows[0]["end_value"] == 70
    assert rows[0]["change"] == -30 and rows[0]["declines"] == 3
    scoped = analytics.execute_plan(plan(metric="clientes_activos", operation="decline"), (2, 3))
    assert scoped["evidence"]["rows"] == []
    assert "0 windows" in scoped["answer"]


@pytest.mark.integration
def test_database_stock_single_month_and_ranking_scope(agent_publication):
    result = analytics.execute_plan(plan(metric="clientes_activos", start="2025-04"), (1, 2))
    assert result["evidence"]["rows"][0]["value"] == 140
    result = analytics.execute_plan(plan(operation="ranking"), (1, 2))
    assert [row["firm_id"] for row in result["evidence"]["rows"]] == [1, 2]
    assert [Decimal(row["value"]) for row in result["evidence"]["rows"]] == [
        Decimal("1.20"),
        Decimal("0.90"),
    ]
    assert result["evidence"]["truncated"] is False


@pytest.mark.integration
def test_database_literal_exact_ambiguous_and_unauthorized_names(agent_publication):
    exact = analytics.execute_plan(plan(firm_name="alpha"))
    assert exact["evidence"]["scope"]["firm_id"] == 1
    ambiguous = analytics.execute_plan(plan(firm_name="Alpha"), (2, 3))
    assert ambiguous["code"] == "ambiguous_firm"
    assert {row["firm_id"] for row in ambiguous["candidates"]} == {2, 3}
    literal = analytics.execute_plan(plan(firm_name="%_!"))
    assert literal["status"] == "answered" and literal["evidence"]["scope"]["firm_id"] == 4
    hidden = analytics.execute_plan(plan(firm_name="Literal %_!"), (1, 2))
    assert hidden["code"] == "firm_not_found" and hidden["candidates"] == []
    mismatch = analytics.execute_plan(plan(firm_name="Alpha East", firm_id=1), (1, 2))
    assert mismatch["code"] == "firm_not_found"


@pytest.mark.integration
def test_database_absent_period_and_null_metric_are_not_zero(agent_publication):
    hours = analytics.execute_plan(plan(metric="horas_trabajadas"), (1,))
    assert hours["evidence"]["rows"][0]["value"] is None
    assert hours["evidence"]["coverage"]["null_metric_firm_months"] == 4
    absent = analytics.execute_plan(plan(start="2026-01", end="2026-02", operation="series"))
    assert len(absent["evidence"]["rows"]) == 2
    assert all(row["value"] is None for row in absent["evidence"]["rows"])
    assert absent["evidence"]["coverage"]["missing_firm_months"] == 8


@pytest.mark.integration
def test_database_reader_transaction_is_readonly_and_repeatable(agent_publication, monkeypatch):
    real_connect = analytics.psycopg.connect
    connections = []

    def tracked_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        connections.append(conn)
        execute = conn.execute

        def checked(sql, *args, **kwargs):
            if sql.startswith(("WITH", "SELECT")):
                readonly = execute("SHOW transaction_read_only").fetchone()
                isolation = execute("SHOW transaction_isolation").fetchone()
                assert readonly["transaction_read_only"] == "on"
                assert isolation["transaction_isolation"] == "repeatable read"
            return execute(sql, *args, **kwargs)

        monkeypatch.setattr(conn, "execute", checked)
        return conn

    monkeypatch.setattr(analytics.psycopg, "connect", tracked_connect)
    assert analytics.execute_plan(plan())["status"] == "answered"
    assert len(connections) == 1 and connections[0].closed
