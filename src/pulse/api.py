"""Read-only, bounded reporting endpoints. Money is serialized as exact decimal strings."""

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pulse.db import connect
from pulse.models import FirmMetricPage, NetworkMetricPage
from pulse.parsing import POLICY_VERSION

app = FastAPI(
    title="Pulse",
    version="1.0.0",
    description=(
        "Phase 1. Monthly advisory-firm metrics with lineage and quality coverage. "
        "All endpoints are read-only. Money uses decimal strings; missing values are null."
    ),
)
STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class Page(BaseModel):
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


class Firm(BaseModel):
    id: int
    nombre: str
    cif: str
    provincia: str
    ciudad: str
    fecha_alta: date
    num_empleados: int
    especialidad: str


class FirmPage(BaseModel):
    items: list[Firm]
    total: int
    limit: int
    offset: int


def connection():
    with connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout = '5s'")
        yield conn


DB = Annotated[Any, Depends(connection)]
Limit = Annotated[int, Query(ge=1, le=500)]
Offset = Annotated[int, Query(ge=0)]


def response(data):
    return JSONResponse(jsonable_encoder(data, custom_encoder={Decimal: str}))


def month_value(value):
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise HTTPException(422, "Month must be YYYY-MM")
    try:
        return date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise HTTPException(422, "Invalid calendar month") from exc


def month_filter(start, end, column="mes"):
    left = month_value(start) if start else date(1900, 1, 1)
    right = month_value(end) if end else date(9999, 12, 1)
    if left > right:
        raise HTTPException(422, "start must not be after end")
    return f"{column} BETWEEN %s AND %s", [left, right]


def page(conn, query, params, limit, offset):
    total = conn.execute(f"SELECT count(*) AS n FROM ({query}) q", params).fetchone()["n"]
    items = conn.execute(query + " LIMIT %s OFFSET %s", [*params, limit, offset]).fetchall()
    return response({"items": items, "total": total, "limit": limit, "offset": offset})


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC / "index.html")


@app.get("/health", tags=["Operations"])
def health(conn: DB):
    conn.execute("SELECT 1 FROM public.schema_migrations LIMIT 1")
    return {"status": "ok"}


@app.get("/api/v1/firms", response_model=FirmPage, tags=["Metrics"])
def firms(conn: DB, search: str = "", limit: Limit = 100, offset: Offset = 0):
    if len(search) > 200:
        raise HTTPException(422, "Search too long")
    return page(
        conn,
        "SELECT * FROM core.firms WHERE nombre ILIKE %s ORDER BY id",
        [f"%{search}%"],
        limit,
        offset,
    )


@app.get("/api/v1/firms/{firm_id}/metrics", response_model=FirmMetricPage, tags=["Metrics"])
def firm_metrics(
    firm_id: int,
    conn: DB,
    start: str | None = None,
    end: str | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
):
    if not conn.execute("SELECT 1 FROM core.firms WHERE id=%s", (firm_id,)).fetchone():
        raise HTTPException(404, "Firm not found")
    clause, args = month_filter(start, end, "c.mes")
    return page(
        conn,
        f"""SELECT c.mes,c.asesoria_id,c.status AS coverage_status,
        f.clientes_activos,f.clientes_nuevos,f.clientes_baja,
        f.declaraciones_renta,f.declaraciones_iva,f.declaraciones_sociedades,f.declaraciones_otros,
        f.facturacion_asesoria_eur,f.facturacion_gestion_eur,f.facturacion_consultoria_eur,
        f.facturacion_otros_eur,
        f.facturacion_asesoria_eur+f.facturacion_gestion_eur+
        f.facturacion_consultoria_eur+f.facturacion_otros_eur AS revenue_eur,
        f.horas_trabajadas,f.consultas_recibidas,f.consultas_resueltas,
        f.incidencias_aeat,f.documentos_procesados,f.satisfaccion_cliente,
        f.has_rejected_newer_delivery,f.source_record_id,d.filename,r.line_number
        FROM mart.coverage c LEFT JOIN mart.firm_month f
          ON f.asesoria_id=c.asesoria_id AND f.mes=c.mes
        LEFT JOIN raw.records r ON r.id=f.source_record_id
        LEFT JOIN raw.deliveries d ON d.id=r.delivery_id
        WHERE c.asesoria_id=%s AND {clause} ORDER BY c.mes""",
        [firm_id, *args],
        limit,
        offset,
    )


@app.get("/api/v1/network/metrics", response_model=NetworkMetricPage, tags=["Metrics"])
def network(
    conn: DB,
    start: str | None = None,
    end: str | None = None,
    limit: Limit = 100,
    offset: Offset = 0,
):
    clause, args = month_filter(start, end)
    return page(
        conn, f"SELECT * FROM mart.network_month WHERE {clause} ORDER BY mes", args, limit, offset
    )


@app.get("/api/v1/quality/summary", tags=["Quality"])
def summary(conn: DB):
    counts = conn.execute("""SELECT count(*) AS raw_rows,
        count(*) FILTER (WHERE status='accepted') AS accepted_rows,
        count(*) FILTER (WHERE status='rejected') AS rejected_rows,
        count(*) FILTER (WHERE status='duplicate') AS duplicate_rows FROM raw.records""").fetchone()
    counts.update(
        conn.execute("""SELECT count(*) AS curated_rows,
        count(*) FILTER (WHERE has_rejected_newer_delivery) AS fallback_rows
        FROM mart.firm_month""").fetchone()
    )
    counts["missing_firm_months"] = conn.execute(
        "SELECT count(*) AS n FROM mart.coverage WHERE status='missing'"
    ).fetchone()["n"]
    counts["warning_rows"] = conn.execute(
        "SELECT count(DISTINCT record_id) AS n FROM ops.issues WHERE severity='warning'"
    ).fetchone()["n"]
    counts["file_errors"] = conn.execute(
        "SELECT count(*) AS n FROM ops.file_errors WHERE resolved_at IS NULL"
    ).fetchone()["n"]
    counts["files"] = conn.execute("SELECT count(*) AS n FROM raw.deliveries").fetchone()["n"]
    counts["latest_run"] = conn.execute(
        "SELECT * FROM ops.runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    counts["last_successful_load"] = conn.execute("""SELECT max(finished_at) AS t FROM ops.runs
        WHERE status IN ('success','partial') AND files_loaded>0""").fetchone()["t"]
    counts["policy_version"] = POLICY_VERSION
    return response(counts)


@app.get("/api/v1/quality/runs", response_model=Page, tags=["Quality"])
def runs(conn: DB, limit: Limit = 50, offset: Offset = 0):
    return page(conn, "SELECT * FROM ops.runs ORDER BY started_at DESC", [], limit, offset)


@app.get("/api/v1/quality/deliveries", response_model=Page, tags=["Quality"])
def deliveries(conn: DB, limit: Limit = 50, offset: Offset = 0):
    return page(
        conn,
        """SELECT d.id,d.filename,d.delivery_month,d.revision,d.sha256,
        d.schema_version,d.policy_version,d.loaded_at,d.run_id,count(r.id) AS raw_rows,
        count(r.id) FILTER (WHERE r.status='accepted') AS accepted_rows,
        count(r.id) FILTER (WHERE r.status='rejected') AS rejected_rows,
        count(r.id) FILTER (WHERE r.status='duplicate') AS duplicate_rows
        FROM raw.deliveries d LEFT JOIN raw.records r ON r.delivery_id=d.id
        GROUP BY d.id ORDER BY d.delivery_month DESC,d.revision DESC""",
        [],
        limit,
        offset,
    )


@app.get("/api/v1/quality/issues", response_model=Page, tags=["Quality"])
def issues(
    conn: DB,
    severity: str | None = None,
    firm_id: int | None = None,
    limit: Limit = 50,
    offset: Offset = 0,
):
    if severity and severity not in ("error", "warning", "info"):
        raise HTTPException(422, "severity must be error, warning or info")
    return page(
        conn,
        """SELECT i.*,r.line_number,r.status,r.payload,r.normalized,d.filename
        FROM ops.issues i JOIN raw.records r ON r.id=i.record_id
        JOIN raw.deliveries d ON d.id=r.delivery_id
        WHERE (%s::text IS NULL OR i.severity=%s)
          AND (%s::text IS NULL OR r.normalized->>'asesoria_id'=%s)
        ORDER BY d.delivery_month DESC,d.revision DESC,r.line_number,i.id""",
        [severity, severity, str(firm_id) if firm_id else None, str(firm_id)],
        limit,
        offset,
    )


@app.get("/api/v1/quality/gaps", response_model=Page, tags=["Quality"])
def gaps(conn: DB, limit: Limit = 50, offset: Offset = 0):
    return page(
        conn,
        """SELECT c.*,f.nombre FROM mart.coverage c
        JOIN core.firms f ON f.id=c.asesoria_id
        WHERE c.status='missing' ORDER BY c.mes,c.asesoria_id""",
        [],
        limit,
        offset,
    )


@app.get("/api/v1/quality/file-errors", response_model=Page, tags=["Quality"])
def file_errors(conn: DB, limit: Limit = 50, offset: Offset = 0):
    return page(
        conn,
        """SELECT id,filename,sha256,reason,run_id,first_seen_at,last_seen_at,
        resolved_at FROM ops.file_errors ORDER BY last_seen_at DESC""",
        [],
        limit,
        offset,
    )


@app.get("/api/v1/lineage/{firm_id}/{month}", tags=["Lineage"])
def lineage(firm_id: int, month: str, conn: DB):
    metric_month = month_value(month)
    winner = conn.execute(
        """SELECT source_record_id FROM mart.firm_month
        WHERE asesoria_id=%s AND mes=%s""",
        (firm_id, metric_month),
    ).fetchone()
    rows = conn.execute(
        """SELECT r.*,d.filename,d.delivery_month,d.revision,d.sha256,
        coalesce((SELECT jsonb_agg(jsonb_build_object('severity',severity,'code',code,
          'field',field,'message',message)) FROM ops.issues i
          WHERE i.record_id=r.id),'[]') AS issues
        FROM raw.records r JOIN raw.deliveries d ON d.id=r.delivery_id
        WHERE r.normalized->>'asesoria_id'=%s AND r.normalized->>'mes'=%s
        ORDER BY d.delivery_month DESC,d.revision DESC,r.line_number LIMIT 500""",
        (str(firm_id), str(metric_month)),
    ).fetchall()
    if not rows:
        raise HTTPException(404, "No source rows for this firm-month")
    return response(
        {
            "asesoria_id": firm_id,
            "mes": metric_month,
            "selected_record_id": winner["source_record_id"] if winner else None,
            "versions": rows,
        }
    )
