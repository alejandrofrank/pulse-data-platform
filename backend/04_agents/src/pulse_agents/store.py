"""Durable request identity, conversation ownership and shared admission control.

This connection can write only agent operational tables. It is never supplied to
an analytical tool or to the model, and is closed before any provider request.
"""

import hashlib
import json
import os
from decimal import Decimal
from uuid import uuid4

from psycopg import connect
from psycopg.rows import dict_row

ADMISSION_LOCK = 7281903
RESERVATION = Decimal("0.01")
HISTORY_MAX_LIMIT = 100
HISTORY_DETAIL_MAX_LIMIT = 10
HISTORY_MAX_OFFSET = 10_000

# Visibility covers ALL turns, not just the requested page or the last plan.
# Questions and compact state can themselves carry privileged information.
# Unknown legacy scope requires full operational permission, even for business.
_VISIBLE_CONVERSATION = """
    c.owner=%(owner)s
    AND (
        (%(firms)s::bigint[] IS NULL AND %(investigate)s)
        OR EXISTS (SELECT 1 FROM agents.runs r WHERE r.conversation_id=c.id)
    )
    AND NOT EXISTS (
        SELECT 1 FROM agents.runs r WHERE r.conversation_id=c.id AND (
            r.owner<>c.owner OR r.profile<>c.profile OR NOT (
                CASE WHEN r.authorization_can_investigate IS NULL
                    THEN %(firms)s::bigint[] IS NULL AND %(investigate)s
                    ELSE (
                        %(firms)s::bigint[] IS NULL OR (
                            r.authorization_firm_ids IS NOT NULL
                            AND r.authorization_firm_ids <@ %(firms)s::bigint[]
                        )
                    ) AND (NOT r.authorization_can_investigate OR %(investigate)s)
                END
            )
        )
    )
"""
_CONVERSATION_COLUMNS = """
    c.id,c.profile AS agent,c.created_at,c.updated_at,
    coalesce((SELECT left(r.question,100) FROM agents.runs r
        WHERE r.conversation_id=c.id ORDER BY r.created_at,r.id LIMIT 1),'') AS title,
    (SELECT count(*) FROM agents.runs r WHERE r.conversation_id=c.id) AS turn_count
"""


class AdmissionError(ValueError):
    def __init__(self, message, status_code=429):
        super().__init__(message)
        self.status_code = status_code


def audit_connection():
    url = os.environ.get("PULSE_AGENT_AUDIT_URL")
    if not url:
        raise AdmissionError("Agent audit storage is not configured", 503)
    conn = connect(url, row_factory=dict_row, connect_timeout=5)
    try:
        if conn.info.user != "pulse_agent_writer" and not conn.info.dbname.endswith("_test"):
            raise AdmissionError("Agent audit connection has an unsafe role", 503)
        conn.execute("SET LOCAL statement_timeout='5s'")
        conn.execute("SET LOCAL lock_timeout='2s'")
        return conn
    except BaseException:
        conn.close()
        raise


def _history_params(owner, authorization):
    # Missing caller authorization is never assumed to mean operator access.
    firms = authorization["firms"] if authorization is not None else ()
    return {
        "owner": owner,
        "firms": list(firms) if firms is not None else None,
        "investigate": authorization["investigate"] if authorization is not None else False,
    }


def _pagination(limit, offset, *, max_limit=HISTORY_MAX_LIMIT):
    if (
        type(limit) is not int
        or type(offset) is not int
        or not 1 <= limit <= max_limit
        or not 0 <= offset <= HISTORY_MAX_OFFSET
    ):
        raise AdmissionError("Invalid history pagination", 422)
    return {"limit": limit, "offset": offset}


def list_conversations(owner, *, authorization, limit=30, offset=0):
    params = {**_history_params(owner, authorization), **_pagination(limit, offset)}
    with audit_connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        total = conn.execute(
            f"SELECT count(*) AS n FROM agents.conversations c WHERE {_VISIBLE_CONVERSATION}",
            params,
        ).fetchone()["n"]
        items = conn.execute(
            f"""SELECT {_CONVERSATION_COLUMNS} FROM agents.conversations c
            WHERE {_VISIBLE_CONVERSATION}
            ORDER BY c.updated_at DESC,c.id DESC LIMIT %(limit)s OFFSET %(offset)s""",
            params,
        ).fetchall()
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_conversation(conversation_id, owner, *, authorization, limit=10, offset=0):
    params = {
        **_history_params(owner, authorization),
        **_pagination(limit, offset, max_limit=HISTORY_DETAIL_MAX_LIMIT),
        "conversation_id": conversation_id,
    }
    with audit_connection() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conversation = conn.execute(
            f"""SELECT {_CONVERSATION_COLUMNS} FROM agents.conversations c
            WHERE c.id=%(conversation_id)s AND {_VISIBLE_CONVERSATION}""",
            params,
        ).fetchone()
        if not conversation:
            raise AdmissionError("Conversation not found", 404)
        # OFFSET walks backwards in history; reverse only the selected page.
        items = conn.execute(
            """SELECT id AS request_id,question,status,created_at,result
            FROM agents.runs WHERE conversation_id=%(conversation_id)s
            ORDER BY created_at DESC,id DESC LIMIT %(limit)s OFFSET %(offset)s""",
            params,
        ).fetchall()
    total = conversation.pop("turn_count")
    return {**conversation, "items": items[::-1], "total": total, "limit": limit, "offset": offset}


def _require_visible_conversation(conn, conversation_id, owner, authorization):
    if not conn.execute(
        f"""SELECT 1 FROM agents.conversations c
        WHERE c.id=%(conversation_id)s AND {_VISIBLE_CONVERSATION}""",
        {**_history_params(owner, authorization), "conversation_id": conversation_id},
    ).fetchone():
        raise AdmissionError("Conversation not found", 404)


def begin(request_id, conversation_id, profile, question, owner, *, live, authorization=None):
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                str(conversation_id) if conversation_id else None,
                profile,
                question,
                owner,
                authorization,
            ],
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    with audit_connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADMISSION_LOCK,))
        # Recover before looking up retries, so a retry of the interrupted request
        # itself can observe the terminal state. Keep unknown spend reserved.
        conn.execute("""UPDATE agents.runs SET status='error',finished_at=now(),
            result=jsonb_build_object('status','error',
                'answer','Worker interrupted; result unavailable',
                'request_id',id::text,'conversation_id',conversation_id::text,'agent',profile,
                'plan',NULL,'evidence',NULL,'trace',jsonb_build_object('run_id',id::text))
            WHERE status='running' AND created_at < now()-interval '2 minutes'""")
        existing = conn.execute("SELECT * FROM agents.runs WHERE id=%s", (request_id,)).fetchone()
        if existing:
            if existing["owner"] != owner or existing["request_hash"] != fingerprint:
                raise AdmissionError(
                    "Request identifier is already used for a different request", 409
                )
            _require_visible_conversation(conn, existing["conversation_id"], owner, authorization)
            if existing["result"] is not None:
                result = existing["result"]
                result["trace"]["replayed"] = True
                return {"cached": result}
            raise AdmissionError(
                "This request is still running; retry with the same identifier", 409
            )
        active = conn.execute(
            "SELECT count(*) AS n FROM agents.runs WHERE status='running'"
        ).fetchone()["n"]
        if active >= 4:
            raise AdmissionError("All agent workers are busy; retry shortly")
        recent = conn.execute(
            """SELECT count(*) AS n FROM agents.runs
            WHERE owner=%s AND created_at>now()-interval '1 hour'""",
            (owner,),
        ).fetchone()["n"]
        if recent >= 60:
            raise AdmissionError("The operator has reached the hourly request limit")
        reserved = RESERVATION if live else Decimal(0)
        if live:
            spent = conn.execute("""SELECT
                coalesce(sum(coalesce(actual_cost_usd,reserved_cost_usd)),0)
                AS amount FROM agents.runs
                WHERE created_at >= date_trunc('day',now() AT TIME ZONE 'UTC')
                    AT TIME ZONE 'UTC'""").fetchone()["amount"]
            budget = Decimal(os.environ.get("PULSE_AGENT_DAILY_BUDGET_USD", "0.50"))
            if not budget.is_finite() or budget < 0 or spent + reserved > budget:
                raise AdmissionError("The application daily model budget is exhausted")
        if conversation_id:
            conversation = conn.execute(
                "SELECT * FROM agents.conversations WHERE id=%s", (conversation_id,)
            ).fetchone()
            if not conversation or conversation["owner"] != owner:
                raise AdmissionError("Conversation not found", 404)
            _require_visible_conversation(conn, conversation_id, owner, authorization)
            if conversation["profile"] != profile:
                raise AdmissionError("This conversation belongs to the other agent", 409)
            if conn.execute(
                """SELECT 1 FROM agents.runs
                WHERE conversation_id=%s AND status='running'""",
                (conversation_id,),
            ).fetchone():
                raise AdmissionError("This conversation already has an active request", 409)
            state = conversation["state"]
        else:
            conversation_id, state = uuid4(), {}
            conn.execute(
                """INSERT INTO agents.conversations(id,owner,profile)
                VALUES (%s,%s,%s)""",
                (conversation_id, owner, profile),
            )
        conn.execute(
            """INSERT INTO agents.runs
            (id,conversation_id,owner,profile,question,request_hash,status,reserved_cost_usd,
             authorization_firm_ids,authorization_can_investigate)
            VALUES (%s,%s,%s,%s,%s,%s,'running',%s,%s,%s)""",
            (
                request_id,
                conversation_id,
                owner,
                profile,
                question,
                fingerprint,
                reserved,
                list(authorization["firms"])
                if authorization is not None and authorization["firms"] is not None
                else None,
                authorization["investigate"] if authorization is not None else None,
            ),
        )
        conn.execute(
            "UPDATE agents.conversations SET updated_at=now() WHERE id=%s",
            (conversation_id,),
        )
    return {"conversation_id": str(conversation_id), "state": state}


def finish(request_id, result, state):
    from psycopg.types.json import Jsonb

    encoded = json.dumps(result, default=str)
    if len(encoded.encode()) > 1024 * 1024:
        result = {
            **result,
            "status": "error",
            "answer": "Evidence exceeds the response budget",
            "evidence": None,
        }
    trace = result.get("trace", {})
    actual = trace.get("cost_usd") if result["status"] != "error" else None
    if trace.get("mode") == "direct":
        actual = "0.00"
    with audit_connection() as conn:
        run = conn.execute(
            """UPDATE agents.runs SET status=%s,result=%s,actual_cost_usd=%s,
            finished_at=now() WHERE id=%s AND status='running' RETURNING conversation_id""",
            (
                "error" if result["status"] == "error" else "complete",
                Jsonb(json.loads(json.dumps(result, default=str))),
                actual,
                request_id,
            ),
        ).fetchone()
        if not run:
            raise AdmissionError("Run expired before completion", 409)
        conn.execute(
            """UPDATE agents.conversations SET state=%s,updated_at=now() WHERE id=%s""",
            (Jsonb(state), run["conversation_id"]),
        )
    return result
