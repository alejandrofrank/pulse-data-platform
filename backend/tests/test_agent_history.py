"""History HTTP contracts and serial PostgreSQL tests; all providers are blocked.

Run this file alone with TEST_DATABASE_URL pointing at a disposable *_test DB.
The shared database fixture resets that database's schemas, never production.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb

from pulse.db import connect
from pulse_agents import provider, runtime, store
from pulse_api.app import app

HISTORY = "/api/v1/agents/conversations"
ASK = "/api/v1/agents/ask"
STATUS = "/api/v1/agents/status"
BASE = datetime(2026, 1, 1, tzinfo=UTC)
OPERATOR = {"firms": None, "investigate": True}
SCOPED = {"firms": (1,), "investigate": False}
TOKENS = {"alice": "synthetic-history-alice", "bob": "synthetic-history-bob"}


def headers(owner="alice"):
    return {"Authorization": f"Bearer {TOKENS[owner]}", "X-Pulse-Agent": "assistant-v1"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for key in (
        "PULSE_AGENT_IDENTITIES_FILE",
        "PULSE_AGENT_ALLOWED_FIRMS",
        "PULSE_AGENT_AUDIT_URL",
    ):
        monkeypatch.delenv(key, raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("History must not discover credentials, call providers, or execute tools")

    for name in ("_key", "_post", "build_opener", "provider_status", "plan"):
        monkeypatch.setattr(provider, name, forbidden)
    monkeypatch.setattr(runtime, "execute_plan", forbidden)


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
            "firm_ids": None,
            "can_investigate": True,
        }
        for owner, token in TOKENS.items()
    ]
    path = tmp_path / "history-identities.json"

    def update(owner="alice", *, firms=None, investigate=True):
        user = next(value for value in values if value["subject"] == owner)
        user.update(firm_ids=firms, can_investigate=investigate)
        path.write_text(json.dumps(values), encoding="utf-8")

    update()
    monkeypatch.setenv("PULSE_AGENT_IDENTITIES_FILE", str(path))
    return update


@pytest.fixture
def history_database(database, monkeypatch):
    # Exercise every real history read/write as the operational role, not admin.
    writer = make_conninfo(database, user="pulse_agent_writer", password="pulse_agent_writer_local")
    monkeypatch.setenv("PULSE_AGENT_AUDIT_URL", writer)
    return database


def result_for(request_id, conversation_id, *, agent="business", status="answered"):
    return {
        "request_id": str(request_id),
        "conversation_id": str(conversation_id),
        "agent": agent,
        "status": status,
        "answer": "Saved evidence, never recomputed on a read",
        "plan": None,
        "evidence": {"value": "123.45"} if status != "error" else None,
        "trace": {"replayed": False},
    }


def seed(
    *,
    owner="alice",
    authorization=OPERATOR,
    questions=("First question",),
    statuses=None,
    agent="business",
    conversation_id=None,
    updated_at=BASE,
    tied=False,
):
    """Use only after history_database; explicit times make ordering assertions stable."""
    conversation_id = conversation_id or uuid4()
    statuses = statuses or ["complete"] * len(questions)
    request_ids = [UUID(int=index + 1) for index in range(len(questions))]
    # IDs must be globally unique, while preserving deterministic per-conversation order.
    prefix = uuid4().int >> 32 << 32
    request_ids = [UUID(int=prefix + value.int) for value in request_ids]
    with connect() as conn:
        conn.execute(
            """INSERT INTO agents.conversations(id,owner,profile,created_at,updated_at,state)
            VALUES (%s,%s,%s,%s,%s,%s)""",
            (conversation_id, owner, agent, BASE, updated_at, Jsonb({"legacy": "secret context"})),
        )
        for index, (request_id, question, status) in enumerate(
            zip(request_ids, questions, statuses, strict=True)
        ):
            saved = result_for(request_id, conversation_id, agent=agent)
            conn.execute(
                """INSERT INTO agents.runs
                (id,conversation_id,owner,profile,question,request_hash,status,reserved_cost_usd,
                 result,created_at,authorization_firm_ids,authorization_can_investigate)
                VALUES (%s,%s,%s,%s,%s,'seed',%s,0,%s,%s,%s,%s)""",
                (
                    request_id,
                    conversation_id,
                    owner,
                    agent,
                    question,
                    status,
                    Jsonb(saved) if status == "complete" else None,
                    BASE + timedelta(seconds=0 if tied else index),
                    list(authorization["firms"])
                    if authorization is not None and authorization["firms"] is not None
                    else None,
                    authorization["investigate"] if authorization is not None else None,
                ),
            )
    return str(conversation_id), [str(value) for value in request_ids]


def get(client, suffix="", **params):
    response = client.get(HISTORY + suffix, headers=headers(), params=params)
    assert response.headers["Cache-Control"] == "no-store"
    return response


@pytest.mark.parametrize("suffix", ["", "/" + str(uuid4())])
@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 101},
        {"limit": "1.5"},
        {"limit": "bad"},
        {"offset": -1},
        {"offset": 10001},
        {"offset": "bad"},
        {"offset": "0.5"},
        {"offset": "9" * 100},
    ],
)
def test_http_pagination_validation(client, monkeypatch, suffix, params):
    connection = Mock(side_effect=AssertionError("Invalid HTTP input reached storage"))
    monkeypatch.setattr(store, "audit_connection", connection)
    assert get(client, suffix, **params).status_code == 422
    connection.assert_not_called()


def test_invalid_conversation_uuid(client, monkeypatch):
    connection = Mock(side_effect=AssertionError("Invalid UUID reached storage"))
    monkeypatch.setattr(store, "audit_connection", connection)
    assert get(client, "/not-a-uuid").status_code == 422
    connection.assert_not_called()


@pytest.mark.parametrize("limit", [11, 100])
def test_detail_page_budget_is_enforced_at_api_and_store(client, monkeypatch, limit):
    connection = Mock(side_effect=AssertionError("Oversized detail page reached storage"))
    monkeypatch.setattr(store, "audit_connection", connection)
    conversation_id = uuid4()
    assert get(client, f"/{conversation_id}", limit=limit).status_code == 422
    with pytest.raises(store.AdmissionError) as error:
        store.get_conversation(conversation_id, "alice", authorization=OPERATOR, limit=limit)
    assert error.value.status_code == 422
    connection.assert_not_called()


@pytest.mark.parametrize("suffix", ["", "/" + str(uuid4())])
@pytest.mark.parametrize("authorization", [None, "Bearer invalid", "Basic alice"])
def test_http_authentication_before_storage(client, identities, monkeypatch, suffix, authorization):
    connection = Mock(side_effect=AssertionError("Anonymous read reached storage"))
    monkeypatch.setattr(store, "audit_connection", connection)
    response = client.get(
        HISTORY + suffix, headers={"Authorization": authorization} if authorization else {}
    )
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    connection.assert_not_called()


@pytest.mark.parametrize("suffix", ["", "/" + str(uuid4())])
@pytest.mark.parametrize("failure", ["missing-audit", "database-error", "unsafe-role", "scope"])
def test_history_storage_and_configuration_fail_closed(client, monkeypatch, suffix, failure):
    connection = None
    if failure == "database-error":
        monkeypatch.setattr(
            store, "audit_connection", Mock(side_effect=psycopg.OperationalError("secret DSN"))
        )
    elif failure == "unsafe-role":
        connection = Mock(info=SimpleNamespace(user="pulse", dbname="pulse"))
        monkeypatch.setenv("PULSE_AGENT_AUDIT_URL", "synthetic")
        monkeypatch.setattr(store, "connect", Mock(return_value=connection))
    elif failure == "scope":
        monkeypatch.setenv("PULSE_AGENT_ALLOWED_FIRMS", "invalid")
    response = get(client, suffix)
    assert response.status_code == 503
    assert "secret DSN" not in response.text
    if connection is not None:
        connection.execute.assert_not_called()
        connection.close.assert_called_once()


def test_status_scope_digest_tracks_effective_permissions(client, identities, monkeypatch):
    monkeypatch.setattr(provider, "provider_status", lambda: {"mode": "off", "model": None})

    def status():
        response = client.get(STATUS, headers=headers())
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        body = response.json()
        assert body["identity"] == "alice" and body["authentication"] == "token"
        assert len(body["access_scope"]) == 64
        assert set(body["access_scope"]) <= set("0123456789abcdef")
        assert "firms" not in body and "can_investigate" not in body
        return body["access_scope"]

    unrestricted = status()
    identities(firms=[2, 1, 2])
    scoped = status()
    assert scoped != unrestricted
    identities(firms=[1, 2])
    assert status() == scoped
    monkeypatch.setenv("PULSE_AGENT_ALLOWED_FIRMS", "2,1,3")
    assert status() == scoped
    monkeypatch.setenv("PULSE_AGENT_ALLOWED_FIRMS", "1")
    restricted = status()
    assert restricted != scoped
    identities(firms=[1, 2], investigate=False)
    assert status() != restricted
    monkeypatch.setenv("PULSE_AGENT_ALLOWED_FIRMS", "bad")
    assert client.get(STATUS, headers=headers()).status_code == 503


@pytest.mark.integration
class TestHistoryDatabase:
    def test_matching_cached_request_cannot_bypass_later_broader_turn(
        self, history_database, client, identities
    ):
        request_id = uuid4()
        first = store.begin(
            request_id,
            None,
            "business",
            "Scoped first question",
            "alice",
            live=False,
            authorization=SCOPED,
        )
        conversation_id = first["conversation_id"]
        store.finish(request_id, result_for(request_id, conversation_id), {})
        later_id = uuid4()
        store.begin(
            later_id,
            UUID(conversation_id),
            "business",
            "Operational question",
            "alice",
            live=False,
            authorization=OPERATOR,
        )
        store.finish(later_id, result_for(later_id, conversation_id), {})
        identities(firms=[1], investigate=False)
        assert get(client).json()["total"] == 0
        with pytest.raises(store.AdmissionError) as error:
            store.begin(
                request_id,
                None,
                "business",
                "Scoped first question",
                "alice",
                live=False,
                authorization=SCOPED,
            )
        assert error.value.status_code == 404

    def test_legacy_interrupted_result_validates_and_exact_fingerprint_is_preserved(
        self, history_database, client, identities
    ):
        request_id = uuid4()
        question = "Investigate total billed revenue for firm ID 1 in 2025-10."
        authorization = {**OPERATOR, "exact_target": {"firm_id": 1, "month": "2025-10"}}
        first = store.begin(
            request_id,
            None,
            "investigation",
            question,
            "alice",
            live=False,
            authorization=authorization,
        )
        conversation_id = first["conversation_id"]
        with connect() as conn:
            conn.execute("UPDATE agents.runs SET created_at=now()-interval '3 minutes'")
            fingerprint = conn.execute("SELECT request_hash FROM agents.runs").fetchone()[
                "request_hash"
            ]
        expected = hashlib.sha256(
            json.dumps(
                [None, "investigation", question, "alice", authorization], ensure_ascii=False
            ).encode()
        ).hexdigest()
        assert fingerprint == expected
        recovered = store.begin(
            request_id,
            None,
            "investigation",
            question,
            "alice",
            live=False,
            authorization=authorization,
        )["cached"]
        assert recovered["status"] == "error"
        with connect() as conn:
            # Simulate legacy unknown scope without rewriting its saved result or hash.
            conn.execute("UPDATE agents.runs SET authorization_can_investigate=NULL")
            saved = conn.execute("SELECT result FROM agents.runs").fetchone()["result"]
        detail = get(client, f"/{conversation_id}")
        assert detail.status_code == 200, detail.text
        turn = detail.json()["items"][0]
        assert turn["status"] == "error" and turn["result"] == saved
        assert saved["plan"] is None and saved["evidence"] is None

    def test_list_and_detail_pagination(self, history_database, client, identities):
        title = "Á" * 120
        first, ids = seed(questions=(title, "second", "third", "fourth", "fifth"), tied=True)
        second, _ = seed()
        newest, _ = seed(updated_at=BASE + timedelta(days=1))
        seed(owner="bob")
        expected = [newest, *sorted([first, second], reverse=True)]
        page = get(client).json()
        assert (page["total"], page["limit"], page["offset"]) == (3, 30, 0)
        assert [item["id"] for item in page["items"]] == expected
        for offset, conversation_id in enumerate(expected):
            page = get(client, limit=1, offset=offset).json()
            assert page["total"] == 3 and page["limit"] == 1 and page["offset"] == offset
            assert page["items"][0]["id"] == conversation_id
            assert set(page["items"][0]) == {
                "id",
                "agent",
                "title",
                "created_at",
                "updated_at",
                "turn_count",
            }
        empty = get(client, limit=100, offset=10000).json()
        assert empty == {"items": [], "total": 3, "limit": 100, "offset": 10000}
        for offset, expected_ids in [(0, ids[3:]), (2, ids[1:3]), (4, ids[:1]), (6, [])]:
            response = get(client, f"/{first}", limit=2, offset=offset)
            assert response.status_code == 200, response.text
            detail = response.json()
            assert detail["title"] == title[:100]
            assert (detail["total"], detail["limit"], detail["offset"]) == (5, 2, offset)
            assert [turn["request_id"] for turn in detail["items"]] == expected_ids
            assert "state" not in detail and "turn_count" not in detail
        assert get(client, f"/{first}").json()["limit"] == 10

    def test_owner_isolation_and_indistinguishable_not_found(
        self, history_database, client, identities
    ):
        mine, _ = seed()
        other, _ = seed(owner="bob", questions=("Private question",))
        assert [item["id"] for item in get(client).json()["items"]] == [mine]
        for conversation_id in (other, str(uuid4())):
            denied = get(client, f"/{conversation_id}")
            assert denied.status_code == 404
            assert denied.json() == {"detail": "Conversation not found"}
        # Caller query parameters cannot substitute for authenticated ownership.
        assert get(client, f"/{other}", owner="bob").status_code == 404
        assert client.get(HISTORY, headers=headers("bob")).json()["total"] == 1

    @pytest.mark.parametrize(
        "saved,current_firms,current_investigate,global_firms,visible",
        [
            (SCOPED, [1], False, None, True),
            (SCOPED, [1, 2], False, None, True),
            (SCOPED, None, False, None, True),
            (SCOPED, [2], False, None, False),
            (SCOPED, [], False, None, False),
            (SCOPED, [1, 2], False, "2", False),
            (SCOPED, None, False, "1", True),
            ({"firms": (1, 2), "investigate": False}, [1], False, None, False),
            (OPERATOR, None, True, None, True),
            (OPERATOR, None, False, None, False),
            (OPERATOR, [1], True, None, False),
            (OPERATOR, None, True, "1", False),
            ({"firms": (), "investigate": False}, [], False, None, True),
        ],
    )
    def test_scope_coverage(
        self,
        history_database,
        client,
        identities,
        monkeypatch,
        saved,
        current_firms,
        current_investigate,
        global_firms,
        visible,
    ):
        conversation_id, _ = seed(authorization=saved)
        identities(firms=current_firms, investigate=current_investigate)
        if global_firms:
            monkeypatch.setenv("PULSE_AGENT_ALLOWED_FIRMS", global_firms)
        assert get(client).json()["total"] == int(visible)
        assert get(client, f"/{conversation_id}").status_code == (200 if visible else 404)

    @pytest.mark.parametrize("agent", ["business", "investigation"])
    @pytest.mark.parametrize(
        "firms,investigate,global_firms,visible",
        [
            (None, True, None, True),
            (None, False, None, False),
            ([1], True, None, False),
            (None, True, "1", False),
        ],
    )
    def test_legacy_requires_unrestricted_operator(
        self,
        history_database,
        client,
        identities,
        monkeypatch,
        agent,
        firms,
        investigate,
        global_firms,
        visible,
    ):
        conversation_id, _ = seed(authorization=None, agent=agent)
        identities(firms=firms, investigate=investigate)
        if global_firms:
            monkeypatch.setenv("PULSE_AGENT_ALLOWED_FIRMS", global_firms)
        assert get(client).json()["total"] == int(visible)
        assert get(client, f"/{conversation_id}").status_code == (200 if visible else 404)

    @pytest.mark.parametrize("legacy", [False, True])
    def test_one_inaccessible_old_turn_hides_whole_conversation_and_state(
        self, history_database, client, identities, legacy
    ):
        conversation_id, ids = seed(authorization=SCOPED, questions=("Private title", "Safe turn"))
        with connect() as conn:
            conn.execute(
                """UPDATE agents.runs SET authorization_firm_ids=NULL,
                authorization_can_investigate=%s WHERE id=%s""",
                (None if legacy else True, ids[0]),
            )
        identities(firms=[1], investigate=False)
        assert get(client, limit=1).json()["total"] == 0
        for offset in (0, 1, 10000):
            assert get(client, f"/{conversation_id}", limit=1, offset=offset).status_code == 404
        with pytest.raises(store.AdmissionError) as error:
            store.begin(
                uuid4(),
                UUID(conversation_id),
                "business",
                "Continue",
                "alice",
                live=False,
                authorization=SCOPED,
            )
        assert error.value.status_code == 404
        with connect() as conn:
            assert conn.execute("SELECT count(*) AS n FROM agents.runs").fetchone()["n"] == 2

    def test_reads_preserve_pending_and_null_error_without_recovery(
        self, history_database, client, identities
    ):
        conversation_id, _ = seed(
            questions=("Running old request", "Failed request"), statuses=("running", "error")
        )
        with connect() as conn:
            before = conn.execute("SELECT * FROM agents.runs ORDER BY id").fetchall()
        for _ in range(2):
            detail = get(client, f"/{conversation_id}").json()
            assert [item["status"] for item in detail["items"]] == ["running", "error"]
            assert all(item["result"] is None for item in detail["items"])
            assert get(client).json()["items"][0]["turn_count"] == 2
        with connect() as conn:
            assert conn.execute("SELECT * FROM agents.runs ORDER BY id").fetchall() == before

    def test_new_runs_record_scope_and_pending_updates_recency(
        self, history_database, client, identities, monkeypatch
    ):
        identities(firms=[1, 2, 3], investigate=False)
        monkeypatch.setenv("PULSE_AGENT_ALLOWED_FIRMS", "2,3,4")
        permissions = runtime.effective_permissions((1, 2, 3), False)
        conversation_id, _ = seed(authorization={"firms": (2,), "investigate": False})
        other, _ = seed(authorization=SCOPED, updated_at=BASE + timedelta(days=1))
        request_id = uuid4()
        admission = store.begin(
            request_id,
            UUID(conversation_id),
            "business",
            "Continue",
            "alice",
            live=False,
            authorization=permissions,
        )
        assert admission["state"] == {"legacy": "secret context"}
        with connect() as conn:
            run = conn.execute("SELECT * FROM agents.runs WHERE id=%s", (request_id,)).fetchone()
        assert run["authorization_firm_ids"] == [2, 3]
        assert run["authorization_can_investigate"] is False
        assert run["result"] is None and run["status"] == "running"
        assert get(client).json()["items"][0]["id"] == conversation_id
        assert get(client, f"/{other}").status_code == 404

    def test_restore_uses_bounded_state_and_reads_never_plan_or_reexecute(
        self, history_database, client, identities, monkeypatch
    ):
        identities(firms=[1], investigate=False)
        plan = {
            "action": "query",
            "metric": "revenue_eur",
            "operation": "total",
            "start": "2025-10",
            "end": "2025-10",
        }
        planner = Mock(return_value={"plan": plan, "trace": {"mode": "offline"}})
        provider_status = Mock(return_value={"mode": "offline", "model": None})
        tool = Mock(
            return_value={
                "status": "answered",
                "answer": "Evidence marker",
                "evidence": {"value": "123.45"},
            }
        )
        monkeypatch.setattr(provider, "plan", planner)
        monkeypatch.setattr(provider, "provider_status", provider_status)
        monkeypatch.setattr(runtime, "execute_plan", tool)
        conversation_id = None
        previous_result = None
        bodies = []
        for index in range(4):
            body = {
                "request_id": str(uuid4()),
                "conversation_id": conversation_id,
                "agent": "business",
                "question": f"Question {index}",
            }
            response = client.post(ASK, headers=headers(), json=body)
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["status"] == "answered"
            context = planner.call_args.args[2]
            assert context["recent_questions"] == [entry["question"] for entry in bodies[-2:]]
            assert context["previous_plan"] == (
                previous_result["plan"] if previous_result else None
            )
            assert "Evidence marker" not in json.dumps(context)
            bodies.append(body)
            conversation_id, previous_result = result["conversation_id"], result
            with TestClient(app) as restored:
                detail = get(restored, f"/{conversation_id}").json()
                assert detail["items"][-1]["result"] == result
            assert planner.call_count == tool.call_count == provider_status.call_count == index + 1
        with connect() as conn:
            state = conn.execute("SELECT state FROM agents.conversations").fetchone()["state"]
            run = conn.execute(
                "SELECT request_hash FROM agents.runs WHERE id=%s", (bodies[-1]["request_id"],)
            ).fetchone()
        assert state == {
            "last_plan": previous_result["plan"],
            "recent_questions": ["Question 2", "Question 3"],
        }
        expected_hash = hashlib.sha256(
            json.dumps(
                [
                    bodies[-1]["conversation_id"],
                    "business",
                    "Question 3",
                    "alice",
                    SCOPED,
                ],
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        assert run["request_hash"] == expected_hash
        replay = client.post(ASK, headers=headers(), json=bodies[-1])
        assert replay.status_code == 200 and replay.json()["trace"]["replayed"] is True
        assert planner.call_count == tool.call_count == 4
        changed = {**bodies[-1], "question": "Changed"}
        assert client.post(ASK, headers=headers(), json=changed).status_code == 409
        identities(firms=[], investigate=False)
        assert get(client, f"/{conversation_id}").status_code == 404
        assert client.post(ASK, headers=headers(), json=bodies[-1]).status_code == 409
        changed = {**bodies[-1], "request_id": str(uuid4())}
        assert client.post(ASK, headers=headers(), json=changed).status_code == 404
        assert planner.call_count == tool.call_count == 4

    def test_writer_role_is_used_and_reporting_reader_cannot_read_history(
        self, history_database, client, identities
    ):
        seed()
        with store.audit_connection() as conn:
            assert (
                conn.execute("SELECT current_user AS role").fetchone()["role"]
                == "pulse_agent_writer"
            )
            assert (
                conn.execute(
                    "SELECT has_schema_privilege(current_user,'mart','USAGE') AS allowed"
                ).fetchone()["allowed"]
                is False
            )
        assert get(client).status_code == 200
        reader = make_conninfo(history_database, user="pulse_reader", password="pulse_reader_local")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), connect(reader) as conn:
            conn.execute("SELECT * FROM agents.runs")

    @pytest.mark.parametrize("mismatch", ["owner", "profile"])
    def test_denormalized_foreign_run_cannot_leak_even_to_operator(
        self, history_database, client, identities, mismatch
    ):
        conversation_id, _ = seed()
        with connect() as conn:
            if mismatch == "owner":
                conn.execute("UPDATE agents.runs SET owner='bob'")
            else:
                conn.execute("UPDATE agents.runs SET profile='investigation'")
        assert get(client).json()["total"] == 0
        assert get(client, f"/{conversation_id}").status_code == 404

    def test_legacy_empty_conversation_state_is_not_resumed_by_scoped_owner(
        self, history_database, client, identities
    ):
        conversation_id, _ = seed(questions=())
        identities(firms=[1], investigate=False)
        assert get(client, f"/{conversation_id}").status_code == 404
        with pytest.raises(store.AdmissionError) as error:
            store.begin(
                uuid4(),
                UUID(conversation_id),
                "business",
                "Resume",
                "alice",
                live=False,
                authorization=SCOPED,
            )
        assert error.value.status_code == 404
        identities()
        assert get(client, f"/{conversation_id}").json()["items"] == []
