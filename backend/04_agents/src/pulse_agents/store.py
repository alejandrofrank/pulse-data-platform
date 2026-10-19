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
        spent = conn.execute("""SELECT coalesce(sum(coalesce(actual_cost_usd,reserved_cost_usd)),0)
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
            (id,conversation_id,owner,profile,question,request_hash,status,reserved_cost_usd)
            VALUES (%s,%s,%s,%s,%s,%s,'running',%s)""",
            (request_id, conversation_id, owner, profile, question, fingerprint, reserved),
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
    actual = result.get("trace", {}).get("cost_usd") if result["status"] != "error" else None
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
