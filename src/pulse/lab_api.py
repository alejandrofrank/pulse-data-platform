"""Local-only playback controls, with a separate writer and reused read-only reporting API."""

import os
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from pulse.api import DB, STATIC, response, router
from pulse.lab import LabConflict, Playback


@asynccontextmanager
async def lifespan(app):
    playback = Playback(os.environ["LAB_DATABASE_URL"], os.environ.get("PULSE_DATA_DIR", "data"))
    playback.start()
    app.state.playback = playback
    try:
        yield
    finally:
        playback.stop()


app = FastAPI(title="Pulse playback lab", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(router)


def controller(request: Request):
    return request.app.state.playback


Lab = Annotated[Playback, Depends(controller)]


def local_action(request: Request, x_pulse_lab: Annotated[str | None, Header()] = None):
    # Custom header blocks browser form submissions; no CORS permission is granted.
    if x_pulse_lab != "playback-v1":
        raise HTTPException(403, "Lab action header required")
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Lab actions must be same-origin")


class PlayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    filenames: list[Annotated[str, Field(min_length=1, max_length=255)]] = Field(
        min_length=1, max_length=500
    )
    interval_ms: int = Field(default=1000, ge=0, le=10000, strict=True)


class ResetRequest(BaseModel):
    confirmation: Literal["RESET LAB"]


def action(call):
    try:
        return response(call())
    except LabConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/", include_in_schema=False)
def lab_page():
    return FileResponse(STATIC / "lab.html")


@app.get("/api/lab/state")
def state(lab: Lab):
    return response(lab.state())


@app.get("/dashboard", include_in_schema=False)
def main_dashboard():
    return RedirectResponse(
        f"http://127.0.0.1:{int(os.environ.get('PULSE_DASHBOARD_PORT', '8000'))}"
    )


@app.get("/api/lab/jobs/{job_id}")
def job_detail(job_id: UUID, lab: Lab):
    return action(lambda: lab.get_job(job_id))


@app.get("/api/lab/health")
def health(lab: Lab, conn: DB):
    conn.execute("SELECT 1 FROM public.schema_migrations LIMIT 1")
    if lab.worker_error or not lab.thread or not lab.thread.is_alive():
        raise HTTPException(503, "Playback worker is not running")
    return {"status": "ok"}


@app.post("/api/lab/play", dependencies=[Depends(local_action)])
def play(body: PlayRequest, lab: Lab):
    return action(lambda: lab.enqueue(body.request_id, body.filenames, body.interval_ms))


@app.post("/api/lab/jobs/{job_id}/{command}", dependencies=[Depends(local_action)])
def control(job_id: UUID, command: Literal["pause", "resume", "cancel"], lab: Lab):
    return action(lambda: lab.control(job_id, command))


@app.post("/api/lab/reset", dependencies=[Depends(local_action)])
def reset(body: ResetRequest, lab: Lab):
    def perform():
        lab.reset()
        return {"status": "empty"}

    return action(perform)
