"""HTTP adapter and server-owned identities; no browser-supplied scope or tools."""

import hashlib
import hmac
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pulse_agents import runtime, store
from pulse_agents.schemas import InvestigationTarget, QueryPlan
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


class ExactInvestigationRequest(InvestigationTarget):
    request_id: UUID
    conversation_id: UUID | None = None
    agent: Literal["investigation"] = "investigation"


class ConversationSummary(BaseModel):
    id: UUID
    agent: Literal["business", "investigation"]
    title: str
    created_at: datetime
    updated_at: datetime
    turn_count: int


class ConversationPage(BaseModel):
    items: list[ConversationSummary]
    total: int
    limit: int
    offset: int


class ConversationTurn(BaseModel):
    request_id: UUID
    question: str
    status: Literal["running", "complete", "error"]
    created_at: datetime
    result: AskResponse | None


class ConversationDetail(BaseModel):
    id: UUID
    agent: Literal["business", "investigation"]
    title: str
    created_at: datetime
    updated_at: datetime
    items: list[ConversationTurn]
    total: int
    limit: int
    offset: int


class HistoryRoute(APIRoute):
    """Never cache history, including authentication and validation failures."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def no_store(request):
            try:
                response = await handler(request)
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            except RequestValidationError as exc:
                response = await request_validation_exception_handler(request, exc)
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store


history_router = APIRouter(route_class=HistoryRoute)
HistoryLimit = Annotated[int, Query(ge=1, le=store.HISTORY_MAX_LIMIT)]
HistoryDetailLimit = Annotated[int, Query(ge=1, le=store.HISTORY_DETAIL_MAX_LIMIT)]
HistoryOffset = Annotated[int, Query(ge=0, le=store.HISTORY_MAX_OFFSET)]


def history_permissions(principal):
    return runtime.effective_permissions(
        principal["allowed_firm_ids"], principal["can_investigate"]
    )


@history_router.get("/conversations", response_model=ConversationPage)
def list_conversations(
    principal: Annotated[dict, Depends(identity)],
    limit: HistoryLimit = 30,
    offset: HistoryOffset = 0,
):
    try:
        return store.list_conversations(
            principal["owner"],
            authorization=history_permissions(principal),
            limit=limit,
            offset=offset,
        )
    except AdmissionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except (psycopg.Error, ValueError, OSError) as exc:
        raise HTTPException(503, "Agent storage or configuration is unavailable") from exc


@history_router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: UUID,
    principal: Annotated[dict, Depends(identity)],
    limit: HistoryDetailLimit = 10,
    offset: HistoryOffset = 0,
):
    try:
        return store.get_conversation(
            conversation_id,
            principal["owner"],
            authorization=history_permissions(principal),
            limit=limit,
            offset=offset,
        )
    except AdmissionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except (psycopg.Error, ValueError, OSError) as exc:
        raise HTTPException(503, "Agent storage or configuration is unavailable") from exc


router.include_router(history_router)


@router.get("/status")
def agent_status(principal: Annotated[dict, Depends(identity)]):
    from pulse_api.app import response

    try:
        permissions = history_permissions(principal)
        if permissions["firms"] is not None:
            permissions["firms"] = sorted(set(permissions["firms"]))
        access_scope = hashlib.sha256(
            json.dumps(permissions, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return response(
            {
                **runtime.status(),
                "identity": principal["owner"],
                "authentication": (
                    "token" if os.environ.get("PULSE_AGENT_IDENTITIES_FILE") else "local"
                ),
                "access_scope": access_scope,
                "dataset": "Pulse reporting database",
            },
            headers={"Cache-Control": "no-store"},
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(
            503,
            "Agent storage or configuration is unavailable",
            headers={"Cache-Control": "no-store"},
        ) from exc


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


@router.post("/investigate", dependencies=[Depends(same_origin)], response_model=AskResponse)
def investigate_exact(
    body: ExactInvestigationRequest, principal: Annotated[dict, Depends(identity)]
):
    from pulse_api.app import response

    try:
        result = runtime.ask(
            body.request_id,
            body.conversation_id,
            "investigation",
            "",
            target=InvestigationTarget(firm_id=body.firm_id, month=body.month),
            **principal,
        )
        return response(AskResponse.model_validate(result).model_dump(mode="json"))
    except AdmissionError as exc:
        raise HTTPException(exc.status_code, str(exc), headers={"Retry-After": "5"}) from exc
    except (psycopg.Error, ValueError, OSError) as exc:
        raise HTTPException(503, "Agent storage or configuration is unavailable") from exc
