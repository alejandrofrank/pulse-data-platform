"""HTTP adapter and server-owned identities; no browser-supplied scope or tools."""

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pulse_agents import runtime
from pulse_agents.schemas import QueryPlan
from pulse_agents.store import AdmissionError

router = APIRouter(prefix="/api/v1/agents", tags=["Agents"])


class Identity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=100)
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    firm_ids: list[Annotated[int, Field(strict=True, gt=0)]] | None = Field(
        default=None, max_length=5000
    )
    can_investigate: bool = False


def identity(authorization: Annotated[str | None, Header()] = None):
    path = os.environ.get("PULSE_AGENT_IDENTITIES_FILE")
    if not path:
        return {"owner": "local-operator", "allowed_firm_ids": None, "can_investigate": True}
    try:
        with Path(path).open("rb") as source:
            content = source.read(65537)
        if len(content) > 65536:
            raise ValueError("Oversized identity file")
        users = [Identity.model_validate(value) for value in json.loads(content)]
        if (
            not users
            or len({u.subject for u in users}) != len(users)
            or len({u.token_sha256 for u in users}) != len(users)
        ):
            raise ValueError("Identity subjects must be unique")
    except (OSError, ValueError, ValidationError, TypeError) as exc:
        raise HTTPException(503, "Agent identity configuration is unavailable") from exc
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Agent access token required")
    supplied = hashlib.sha256(authorization[7:].encode()).hexdigest()
    user = next((u for u in users if hmac.compare_digest(u.token_sha256, supplied)), None)
    if not user:
        raise HTTPException(401, "Invalid agent access token")
    return {
        "owner": user.subject,
        "allowed_firm_ids": tuple(sorted(set(user.firm_ids)))
        if user.firm_ids is not None
        else None,
        "can_investigate": user.can_investigate,
    }


def same_origin(request: Request, x_pulse_agent: Annotated[str | None, Header()] = None):
    if x_pulse_agent != "assistant-v1":
        raise HTTPException(403, "Agent action header required")
    origin = request.headers.get("origin")
    if origin and (
        urlsplit(origin).netloc != request.headers.get("host")
        or urlsplit(origin).scheme != request.url.scheme
    ):
        raise HTTPException(403, "Agent requests must be same-origin")


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    conversation_id: UUID | None = None
    agent: Literal["business", "investigation"]
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question")
    @classmethod
    def valid_question(cls, value):
        value = value.strip()
        if not value or "\x00" in value:
            raise ValueError("Question must contain non-NUL text")
        return value


class AskResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    conversation_id: UUID
    agent: Literal["business", "investigation"]
    status: Literal["answered", "clarification", "unsupported", "error"]
    answer: str
    plan: QueryPlan | None
    evidence: dict[str, Any] | None
    trace: dict[str, Any]


@router.get("/status")
def agent_status(principal: Annotated[dict, Depends(identity)]):
    return {
        **runtime.status(),
        "identity": principal["owner"],
        "authentication": "token" if os.environ.get("PULSE_AGENT_IDENTITIES_FILE") else "local",
        "dataset": "Pulse reporting database",
    }


@router.post("/ask", dependencies=[Depends(same_origin)], response_model=AskResponse)
def ask(body: AskRequest, principal: Annotated[dict, Depends(identity)]):
    from pulse_api.app import response

    try:
        result = runtime.ask(
            body.request_id, body.conversation_id, body.agent, body.question, **principal
        )
        return response(AskResponse.model_validate(result).model_dump(mode="json"))
    except AdmissionError as exc:
        raise HTTPException(exc.status_code, str(exc), headers={"Retry-After": "5"}) from exc
    except (psycopg.Error, ValueError, OSError) as exc:
        raise HTTPException(503, "Agent storage or configuration is unavailable") from exc
