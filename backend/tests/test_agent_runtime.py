"""Offline HTTP checks and serial PostgreSQL integration tests for agent execution.

Every provider entry point is mocked, including status/key discovery. Integration
tests use the guarded *_test fixture and must not run alongside another DB suite.
"""

import copy
import hashlib
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_ingestion import source
from test_parsing import make_csv

from pulse.db import connect
from pulse.ingest import ingest
from pulse_agents import analytics, provider, runtime, store
from pulse_api.app import app

ASK = "/api/v1/agents/ask"
STATUS = "/api/v1/agents/status"
ACTION_HEADER = {"X-Pulse-Agent": "assistant-v1"}
FAKE_KEY = "sk-agent-runtime-synthetic-not-a-real-key"
TOKENS = {"alice": "synthetic-alice-token", "bob": "synthetic-bob-token"}
QUESTION = "Network revenue in October 2025"


def planned(**changes):
    return {
        "plan": {
            "action": "query",
            "metric": "revenue_eur",
            "operation": "total",
            "start": "2025-10",
            "end": "2025-10",
            **changes,
        },
        "trace": {
            "model": "synthetic-planner",
            "mode": "live",
            "input_tokens": 10,
            "output_tokens": 20,
            "cost_usd": "0.001",
        },
    }


def request_body(**changes):
    return {
        "request_id": str(uuid4()),
        "conversation_id": None,
        "agent": "business",
        "question": QUESTION,
        **changes,
    }


def headers(owner=None):
    return {
        **ACTION_HEADER,
        **({"Authorization": f"Bearer {TOKENS[owner]}"} if owner else {}),
    }


@pytest.fixture(autouse=True)
def offline_provider(monkeypatch):
    for name in (
        "PULSE_AGENT_IDENTITIES_FILE",
        "PULSE_AGENT_ALLOWED_FIRMS",
        "PULSE_AGENT_AUDIT_URL",
        "OPENAI_API_KEY_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("PULSE_AGENT_DAILY_BUDGET_USD", "0.50")

    def forbidden(*args, **kwargs):
        pytest.fail("Runtime tests must not access provider credentials or make paid calls")

    monkeypatch.setattr(provider, "_key", forbidden)
    monkeypatch.setattr(provider, "_post", forbidden)
    monkeypatch.setattr(provider, "build_opener", forbidden)
    monkeypatch.setattr(
        provider,
        "provider_status",
        Mock(return_value={"mode": "live", "model": "synthetic-planner"}),
    )
    planner = Mock(return_value=planned())
    monkeypatch.setattr(provider, "plan", planner)
    return planner


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


@pytest.fixture
def identities(tmp_path, monkeypatch):
    values = [
        {
            "subject": owner,
            "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "firm_ids": [1] if owner == "alice" else None,
            "can_investigate": owner == "bob",
        }
        for owner, token in TOKENS.items()
    ]
    path = tmp_path / "synthetic-identities.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("PULSE_AGENT_IDENTITIES_FILE", str(path))
    return path


@pytest.fixture
def no_admission(monkeypatch):
    guard = Mock(side_effect=store.AdmissionError("Rejected HTTP input reached admission", 503))
    monkeypatch.setattr(store, "begin", guard)
    return guard


@pytest.fixture
def agent_database(database, monkeypatch):
    monkeypatch.setenv("PULSE_AGENT_AUDIT_URL", database)
    return database


@pytest.fixture
def dataset(agent_database, tmp_path):
    drops = source(tmp_path)
    with (tmp_path / "asesorias.csv").open("a", encoding="utf-8") as master:
        master.write("2,Other firm,B87654321,Sevilla,Sevilla,2020-01-01,8,Fiscal\n")
    (drops / "metricas_2025-10.csv").write_bytes(
        make_csv(
            [
                {"facturacion_asesoria_eur": "120.25"},
                {"asesoria_id": "2", "facturacion_asesoria_eur": "300.00"},
            ]
        )
    )
    ingest(tmp_path)
    return drops


def saved_run(request_id):
    with connect() as conn:
        return conn.execute("SELECT * FROM agents.runs WHERE id=%s", (request_id,)).fetchone()


def run_count():
    with connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM agents.runs").fetchone()["n"]


def published_rows():
    with connect() as conn:
        return conn.execute("SELECT * FROM mart.firm_month ORDER BY asesoria_id,mes").fetchall()


def assert_total(result, expected):
    assert result["status"] == "answered", result
    evidence = result["evidence"]
    assert evidence["publication_id"]
    assert "SELECT" in evidence["sql"]
    assert len(evidence["rows"]) == 1
    assert isinstance(evidence["rows"][0]["value"], str)
    assert Decimal(evidence["rows"][0]["value"]) == Decimal(expected)
    assert evidence["coverage"]["complete"] is True
    assert expected in result["answer"]


def assert_no_credentials(value):
    encoded = json.dumps(value, default=str)
    for secret in (FAKE_KEY, *TOKENS.values()):
        assert secret not in encoded
        assert hashlib.sha256(secret.encode()).hexdigest() not in encoded


def seed_runs(count, *, owner="local-operator", status="complete", cost="0"):
    """Seed admission history only after a test has requested agent_database."""
    conversation = uuid4()
    with connect() as conn:
        conn.execute(
            "INSERT INTO agents.conversations(id,owner,profile) VALUES (%s,%s,'business')",
            (conversation, owner),
        )
        with conn.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO agents.runs
                (id,conversation_id,owner,profile,question,request_hash,status,
                 reserved_cost_usd,actual_cost_usd)
                VALUES (%s,%s,%s,'business','Synthetic prior request','seed',%s,%s,%s)""",
                [
                    (uuid4(), conversation, owner, status, store.RESERVATION, Decimal(cost))
                    for _ in range(count)
                ],
            )


@pytest.mark.parametrize(
    "extra",
    [
        {"sql": "DELETE FROM mart.firm_month"},
        {"scope": [2]},
        {"allowed_firm_ids": [2]},
        {"can_investigate": True},
        {"owner": "bob"},
        {"plan": {"action": "query", "firm_id": 2}},
    ],
    ids=["sql", "scope", "allowed-firms", "operational-access", "owner", "plan"],
)
def test_http_rejects_caller_supplied_authority_or_sql(
    client, offline_provider, no_admission, extra
):
    response = client.post(ASK, headers=headers(), json=request_body(**extra))
    assert response.status_code == 422
    offline_provider.assert_not_called()
    no_admission.assert_not_called()


@pytest.mark.parametrize(
    "request_headers",
    [
        {},
        {"X-Pulse-Agent": "wrong"},
        {**ACTION_HEADER, "Origin": "https://other.example"},
        {**ACTION_HEADER, "Origin": "http://testserver:8080"},
        {**ACTION_HEADER, "Origin": "https://testserver"},
        {**ACTION_HEADER, "Origin": "null"},
    ],
    ids=["missing-header", "wrong-header", "other-host", "other-port", "other-scheme", "null"],
)
def test_csrf_rejected_before_admission(client, offline_provider, no_admission, request_headers):
    response = client.post(ASK, headers=request_headers, json=request_body())
    assert response.status_code == 403
    offline_provider.assert_not_called()
    no_admission.assert_not_called()


@pytest.mark.parametrize("owner", ["alice", "bob"])
def test_configured_identity_status_exposes_no_tokens(client, identities, owner):
    response = client.get(STATUS, headers=headers(owner))
    assert response.status_code == 200
    body = response.json()
    assert body["identity"] == owner
    assert body["authentication"] == "token"
    assert set(body["profiles"]) == {"business", "investigation"}
    assert_no_credentials(body)
    assert str(identities) not in response.text


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-synthetic-token", "Basic alice"])
def test_identity_file_disables_anonymous_fallback(
    client, identities, offline_provider, no_admission, authorization
):
    request_headers = headers()
    if authorization is not None:
        request_headers["Authorization"] = authorization
    response = client.post(ASK, headers=request_headers, json=request_body())
    assert response.status_code == 401
    assert client.get(STATUS, headers=request_headers).status_code == 401
    offline_provider.assert_not_called()
    no_admission.assert_not_called()


@pytest.mark.parametrize("kind", ["missing", "invalid-json", "empty", "duplicate", "oversized"])
def test_bad_identity_file_fails_closed(client, identities, offline_provider, no_admission, kind):
    if kind == "missing":
        identities.unlink()
    elif kind == "invalid-json":
        identities.write_text("{" + FAKE_KEY, encoding="utf-8")
    elif kind == "empty":
        identities.write_text("[]", encoding="utf-8")
    elif kind == "duplicate":
        value = json.loads(identities.read_text(encoding="utf-8"))[0]
        identities.write_text(json.dumps([value, value]), encoding="utf-8")
    else:
        identities.write_text(" " * 65537, encoding="utf-8")
    for response in (
        client.get(STATUS, headers=headers("alice")),
        client.post(ASK, headers=headers("alice"), json=request_body()),
    ):
        assert response.status_code == 503
        assert_no_credentials(response.json())
        assert str(identities) not in response.text
    offline_provider.assert_not_called()
    no_admission.assert_not_called()


def test_audit_storage_requires_explicit_configuration():
    with pytest.raises(store.AdmissionError) as exc:
        store.audit_connection()
    assert exc.value.status_code == 503


def test_audit_owner_connection_outside_test_database_is_rejected(monkeypatch):
    conn = Mock(info=SimpleNamespace(user="owner", dbname="pulse_production"))
    monkeypatch.setenv("PULSE_AGENT_AUDIT_URL", "synthetic-audit-url")
    connector = Mock(return_value=conn)
    monkeypatch.setattr(store, "connect", connector)
    with pytest.raises(store.AdmissionError) as exc:
        store.audit_connection()
    assert exc.value.status_code == 503
    conn.close.assert_called_once_with()
    conn.execute.assert_not_called()


def test_audit_setup_failure_closes_connection_and_sets_connect_timeout(monkeypatch):
    conn = Mock(info=SimpleNamespace(user="pulse_agent_writer", dbname="pulse"))
    conn.execute.side_effect = RuntimeError("Synthetic setup failure")
    connector = Mock(return_value=conn)
    monkeypatch.setenv("PULSE_AGENT_AUDIT_URL", "synthetic-audit-url")
    monkeypatch.setattr(store, "connect", connector)
    with pytest.raises(RuntimeError, match="Synthetic"):
        store.audit_connection()
    assert connector.call_args.kwargs["connect_timeout"] == 5
    conn.close.assert_called_once_with()


@pytest.mark.integration
class TestAgentIntegration:
    def test_permission_revocation_prevents_replaying_a_broader_cached_answer(
        self, dataset, client, identities, offline_provider
    ):
        body = request_body()
        assert client.post(ASK, headers=headers("alice"), json=body).status_code == 200
        values = json.loads(identities.read_text())
        values[0]["firm_ids"] = []
        identities.write_text(json.dumps(values))
        denied = client.post(ASK, headers=headers("alice"), json=body)
        assert denied.status_code == 409
        assert "140.25" not in denied.text
        assert offline_provider.call_count == 1

    def test_investigation_does_not_ignore_a_conflicting_firm_name(
        self, dataset, offline_provider, monkeypatch
    ):
        from pulse_api import investigation

        tool = Mock(side_effect=AssertionError("Must clarify conflicting identity"))
        monkeypatch.setattr(investigation, "investigate", tool)
        offline_provider.return_value = planned(action="investigate", firm_id=1, firm_name="Other")
        result = runtime.ask(uuid4(), None, "investigation", "Investigate Other firm 1")
        assert result["status"] == "clarification"
        tool.assert_not_called()

    def test_expired_request_can_replay_its_terminal_state_without_new_model_call(
        self, agent_database, client, offline_provider
    ):
        body = request_body()
        store.begin(
            UUID(body["request_id"]),
            None,
            "business",
            QUESTION,
            "local-operator",
            live=True,
            authorization={"firms": None, "investigate": True},
        )
        with connect() as conn:
            conn.execute("UPDATE agents.runs SET created_at=now()-interval '3 minutes'")
        result = client.post(ASK, headers=headers(), json=body)
        assert result.status_code == 200
        assert result.json()["status"] == "error"
        assert result.json()["trace"]["replayed"] is True
        offline_provider.assert_not_called()

    def test_http_returns_actual_sql_results_and_persists_audit(
        self, dataset, client, offline_provider
    ):
        body = request_body()
        response = client.post(ASK, headers={**headers(), "Origin": "http://testserver"}, json=body)
        assert response.status_code == 200
        result = response.json()
        assert_total(result, "460.25")
        assert result["request_id"] == body["request_id"]
        UUID(result["conversation_id"])
        assert result["trace"]["replayed"] is False
        run = saved_run(body["request_id"])
        assert run["status"] == "complete"
        assert run["owner"] == "local-operator"
        assert run["result"] == result
        assert run["reserved_cost_usd"] == store.RESERVATION
        assert run["actual_cost_usd"] == Decimal("0.001")
        assert run["finished_at"] is not None
        offline_provider.assert_called_once()
        assert_no_credentials(result)

    def test_identical_request_replays_without_provider_or_query_execution(
        self, dataset, client, offline_provider, monkeypatch
    ):
        body = request_body()
        first = client.post(ASK, headers=headers(), json=body)
        assert first.status_code == 200
        execute = Mock(side_effect=AssertionError("Replay must not execute analytical tools"))
        monkeypatch.setattr(runtime, "execute_plan", execute)
        replay = client.post(ASK, headers=headers(), json=body)
        assert replay.status_code == 200
        expected = copy.deepcopy(first.json())
        expected["trace"]["replayed"] = True
        assert replay.json() == expected
        assert run_count() == 1
        offline_provider.assert_called_once()
        execute.assert_not_called()

    def test_reused_request_id_with_changed_question_is_conflict(
        self, dataset, client, offline_provider
    ):
        body = request_body()
        assert client.post(ASK, headers=headers(), json=body).status_code == 200
        changed = {**body, "question": "Active clients in October 2025"}
        response = client.post(ASK, headers=headers(), json=changed)
        assert response.status_code == 409
        assert response.headers["Retry-After"]
        assert run_count() == 1
        assert saved_run(body["request_id"])["question"] == QUESTION
        offline_provider.assert_called_once()

    def test_conversation_cannot_be_accessed_by_another_owner(
        self, dataset, client, identities, offline_provider
    ):
        first = client.post(ASK, headers=headers("alice"), json=request_body())
        assert first.status_code == 200
        assert_total(first.json(), "140.25")
        response = client.post(
            ASK,
            headers=headers("bob"),
            json=request_body(conversation_id=first.json()["conversation_id"]),
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Conversation not found"
        assert run_count() == 1
        offline_provider.assert_called_once()

    def test_profiles_keep_separate_conversations_and_cannot_switch_in_place(
        self, dataset, client, identities, offline_provider, monkeypatch
    ):
        from pulse_api import investigation

        report = {
            "status": "consistent",
            "findings": [{"code": "api_revenue", "status": "match"}],
            "evidence": {"original_revenue_eur": "140.25"},
        }
        investigate = Mock(return_value=report)
        monkeypatch.setattr(investigation, "investigate", investigate)
        first = client.post(ASK, headers=headers("bob"), json=request_body()).json()
        offline_provider.return_value = planned(action="investigate", firm_id=1)
        conflict = client.post(
            ASK,
            headers=headers("bob"),
            json=request_body(agent="investigation", conversation_id=first["conversation_id"]),
        )
        assert conflict.status_code == 409
        second_response = client.post(
            ASK, headers=headers("bob"), json=request_body(agent="investigation")
        )
        assert second_response.status_code == 200
        second = second_response.json()
        assert second["status"] == "answered"
        assert second["evidence"] == report
        assert second["conversation_id"] != first["conversation_id"]
        investigate.assert_called_once()
        assert offline_provider.call_count == 2
        with connect() as conn:
            conversations = conn.execute(
                "SELECT owner,profile FROM agents.conversations ORDER BY profile"
            ).fetchall()
        assert conversations == [
            {"owner": "bob", "profile": "business"},
            {"owner": "bob", "profile": "investigation"},
        ]

    def test_followup_receives_only_intent_and_retrieves_new_publication(
        self, dataset, tmp_path, client, identities, offline_provider
    ):
        offline_provider.return_value = planned(firm_id=1)
        first = client.post(ASK, headers=headers("alice"), json=request_body()).json()
        assert_total(first, "140.25")
        (dataset / "metricas_2025-10__001.csv").write_bytes(
            make_csv([{"facturacion_asesoria_eur": "200.25"}])
        )
        ingest(tmp_path)
        followup = "And what is the current value for that same firm and month?"
        response = client.post(
            ASK,
            headers=headers("alice"),
            json=request_body(conversation_id=first["conversation_id"], question=followup),
        )
        assert response.status_code == 200
        second = response.json()
        assert_total(second, "220.25")
        assert second["evidence"]["publication_id"] != first["evidence"]["publication_id"]
        assert second["conversation_id"] == first["conversation_id"]
        context = offline_provider.call_args.args[2]
        assert set(context) == {
            "previous_plan",
            "recent_questions",
            "current_date",
            "execution_mode",
        }
        assert context["execution_mode"] == "live"
        assert context["previous_plan"] == first["plan"]
        assert context["recent_questions"] == [QUESTION]
        assert "140.25" not in json.dumps(context)
        assert "sql" not in context and "evidence" not in context
        assert_no_credentials(context)
        with connect() as conn:
            state = conn.execute(
                "SELECT state FROM agents.conversations WHERE id=%s", (second["conversation_id"],)
            ).fetchone()["state"]
        assert state == {"last_plan": second["plan"], "recent_questions": [QUESTION, followup]}
        assert offline_provider.call_count == 2

    def test_server_scope_blocks_model_requested_other_firm(
        self, dataset, identities, client, offline_provider
    ):
        offline_provider.return_value = planned(firm_id=2)
        response = client.post(ASK, headers=headers("alice"), json=request_body())
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "unsupported"
        assert result["evidence"]["rows"] == []
        assert result["evidence"]["sql"] is None
        assert "320.00" not in response.text and "Other firm" not in response.text

    def test_deployment_scope_intersects_identity_scope_without_widening(
        self, dataset, identities, client, monkeypatch
    ):
        monkeypatch.setenv("PULSE_AGENT_ALLOWED_FIRMS", "2")
        response = client.post(ASK, headers=headers("alice"), json=request_body())
        assert response.status_code == 200
        assert response.json()["status"] == "unsupported"
        assert response.json()["evidence"]["rows"] == []

    @pytest.mark.parametrize(
        ("allowed", "permission"), [(None, False), ((1,), True), ((1,), False)]
    )
    def test_investigator_requires_full_operational_permission(
        self, dataset, offline_provider, monkeypatch, allowed, permission
    ):
        from pulse_api import investigation

        tool = Mock(side_effect=AssertionError("Unauthorized investigation tool must not run"))
        monkeypatch.setattr(investigation, "investigate", tool)
        offline_provider.return_value = planned(action="investigate", firm_id=1)
        result = runtime.ask(
            uuid4(),
            None,
            "investigation",
            "Investigate firm 1 in October 2025",
            owner="limited-operator",
            allowed_firm_ids=allowed,
            can_investigate=permission,
        )
        assert result["status"] == "unsupported"
        assert result["evidence"] is None
        tool.assert_not_called()

    @pytest.mark.parametrize(
        "changes",
        [
            {"action": "execute_sql"},
            {"sql": "DELETE FROM mart.firm_month"},
            {"metric": "revenue_eur; DROP SCHEMA mart CASCADE"},
        ],
        ids=["invalid-action", "injected-sql", "injected-identifier"],
    )
    def test_malicious_model_plan_is_safe_error_without_data_mutation(
        self, dataset, client, offline_provider, monkeypatch, changes
    ):
        before = published_rows()
        offline_provider.return_value = planned(**changes)
        tool = Mock(side_effect=AssertionError("Invalid plan must not reach analytics"))
        monkeypatch.setattr(runtime, "execute_plan", tool)
        body = request_body()
        response = client.post(ASK, headers=headers(), json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "error"
        assert result["plan"] is None and result["evidence"] is None
        assert result["trace"]["error_type"] == "ValidationError"
        assert "DROP SCHEMA" not in response.text and "DELETE FROM" not in response.text
        assert published_rows() == before
        assert saved_run(body["request_id"])["status"] == "error"
        tool.assert_not_called()

    def test_provider_failure_persists_safe_error_reserves_cost_and_replays(
        self, dataset, client, offline_provider
    ):
        offline_provider.side_effect = RuntimeError(f"Authorization: Bearer {FAKE_KEY}")
        body = request_body()
        response = client.post(ASK, headers=headers(), json=body)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "error"
        assert result["trace"]["error_type"] == "RuntimeError"
        assert result["trace"]["cost_usd"] is None
        assert result["evidence"] is None
        assert_no_credentials(result)
        run = saved_run(body["request_id"])
        assert run["status"] == "error" and run["result"] == result
        assert run["actual_cost_usd"] is None
        assert run["reserved_cost_usd"] == store.RESERVATION
        assert_no_credentials(run["result"])
        replay = client.post(ASK, headers=headers(), json=body)
        assert replay.status_code == 200
        assert replay.json()["trace"]["replayed"] is True
        offline_provider.assert_called_once()

    def test_failed_provider_reservation_counts_toward_daily_budget(
        self, dataset, client, offline_provider, monkeypatch
    ):
        monkeypatch.setenv("PULSE_AGENT_DAILY_BUDGET_USD", str(store.RESERVATION))
        offline_provider.side_effect = RuntimeError("Synthetic provider failure")
        first = client.post(ASK, headers=headers(), json=request_body())
        assert first.status_code == 200 and first.json()["status"] == "error"
        second = client.post(ASK, headers=headers(), json=request_body())
        assert second.status_code == 429
        assert "budget" in second.json()["detail"]
        assert run_count() == 1
        offline_provider.assert_called_once()

    def test_hourly_limit_rejects_before_provider_and_expires(
        self, dataset, client, offline_provider
    ):
        seed_runs(60)
        rejected = client.post(ASK, headers=headers(), json=request_body())
        assert rejected.status_code == 429
        assert "hourly" in rejected.json()["detail"]
        assert run_count() == 60
        offline_provider.assert_not_called()
        with connect() as conn:
            conn.execute("UPDATE agents.runs SET created_at=now()-interval '61 minutes'")
        accepted = client.post(ASK, headers=headers(), json=request_body())
        assert accepted.status_code == 200
        assert_total(accepted.json(), "460.25")
        offline_provider.assert_called_once()

    def test_daily_budget_is_shared_across_owners(
        self, dataset, identities, client, offline_provider, monkeypatch
    ):
        monkeypatch.setenv("PULSE_AGENT_DAILY_BUDGET_USD", "0.01")
        seed_runs(1, owner="alice", cost="0.01")
        response = client.post(ASK, headers=headers("bob"), json=request_body())
        assert response.status_code == 429
        assert "budget" in response.json()["detail"]
        assert run_count() == 1
        offline_provider.assert_not_called()

    def test_global_active_limit_is_shared_across_owners(
        self, dataset, identities, client, offline_provider
    ):
        for index in range(4):
            seed_runs(1, owner=f"synthetic-busy-{index}", status="running")
        response = client.post(ASK, headers=headers("bob"), json=request_body())
        assert response.status_code == 429
        assert "busy" in response.json()["detail"]
        assert run_count() == 4
        offline_provider.assert_not_called()

    def test_running_request_and_same_conversation_are_not_executed_twice(
        self, agent_database, client, offline_provider
    ):
        request_id = uuid4()
        admission = store.begin(
            request_id,
            None,
            "business",
            QUESTION,
            "local-operator",
            live=True,
            authorization={"firms": None, "investigate": True},
        )
        for body in (
            request_body(request_id=str(request_id)),
            request_body(conversation_id=admission["conversation_id"]),
        ):
            response = client.post(ASK, headers=headers(), json=body)
            assert response.status_code == 409
        assert run_count() == 1
        offline_provider.assert_not_called()

    def test_tools_use_read_only_connection_and_close_audit_before_planning(
        self, dataset, client, offline_provider, monkeypatch
    ):
        audits, readers, read_only = [], [], []
        real_audit = store.audit_connection
        real_connect = analytics.psycopg.connect

        def audit():
            conn = real_audit()
            audits.append(conn)
            return conn

        def reader(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            if "default_transaction_read_only=on" in kwargs.get("options", ""):
                readers.append(conn)
                read_only.append(conn.execute("SHOW transaction_read_only").fetchone())
            return conn

        def plan_after_audit_closed(*args):
            assert audits and all(conn.closed for conn in audits)
            return planned()

        monkeypatch.setattr(store, "audit_connection", audit)
        monkeypatch.setattr(analytics.psycopg, "connect", reader)
        offline_provider.side_effect = plan_after_audit_closed
        response = client.post(ASK, headers=headers(), json=request_body())
        assert response.status_code == 200
        assert_total(response.json(), "460.25")
        assert read_only == [{"transaction_read_only": "on"}]
        assert all(conn.closed for conn in [*audits, *readers])

    def test_reader_and_audit_writer_have_separate_grants(self, agent_database):
        with connect() as conn:
            roles = {
                row["rolname"]
                for row in conn.execute(
                    "SELECT rolname FROM pg_roles "
                    "WHERE rolname IN ('pulse_reader','pulse_agent_writer')"
                ).fetchall()
            }
            if "pulse_reader" not in roles:
                pytest.skip("This test database does not provision the optional pulse_reader role")
            assert "pulse_agent_writer" in roles

            def privilege(role, table, permission):
                return conn.execute(
                    "SELECT has_table_privilege(%s,%s,%s) AS allowed", (role, table, permission)
                ).fetchone()["allowed"]

            for table in ("agents.conversations", "agents.runs"):
                for permission in ("SELECT", "INSERT", "UPDATE"):
                    assert privilege("pulse_agent_writer", table, permission)
                    assert not privilege("pulse_reader", table, permission)
                assert not privilege("pulse_agent_writer", table, "DELETE,TRUNCATE")
            for table in ("mart.firm_month", "raw.records", "ops.publications", "core.firms"):
                assert privilege("pulse_reader", table, "SELECT")
                assert not privilege("pulse_reader", table, "INSERT,UPDATE,DELETE,TRUNCATE")
                assert not privilege("pulse_agent_writer", table, "SELECT,INSERT,UPDATE,DELETE")
