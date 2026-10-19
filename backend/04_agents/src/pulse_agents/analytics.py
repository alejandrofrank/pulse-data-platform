"""Fixed-query analytics over a single read-only publication snapshot.

SQL identifiers/expressions come only from the immutable catalogue below. All
request values, including scope and limits, are driver parameters. Partial sums
are explicitly sums of observed values, never imputed full-network estimates.
No database connection, environment read, or model call happens during compilation.
"""

import os
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from time import monotonic
from types import MappingProxyType
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from .schemas import MAX_MONTHS, Metric, Operation, QueryPlan, month_count

MAX_RESULTS = 100
MAX_NAME_MATCHES = 20
MAX_SCOPE_FIRMS = 10_000
STATEMENT_TIMEOUT_MS = 5_000
EXECUTION_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class MetricDefinition:
    expression: str
    kind: str
    unit: str
    definition: str


METRIC_CATALOGUE = MappingProxyType(
    {
        Metric.REVENUE_EUR: MetricDefinition(
            "m.facturacion_asesoria_eur + m.facturacion_gestion_eur + "
            "m.facturacion_consultoria_eur + m.facturacion_otros_eur",
            "flow",
            "EUR",
            "Sum of the four published billing components: asesoria, gestion, consultoria, otros. "
            "No profit, cash-receipt, tax, or currency-conversion interpretation is inferred.",
        ),
        Metric.CLIENTES_ACTIVOS: MetricDefinition(
            "m.clientes_activos",
            "stock",
            "clients",
            "Published active-client stock for one month; additive across firms, "
            "never across months.",
        ),
        Metric.CLIENTES_NUEVOS: MetricDefinition(
            "m.clientes_nuevos",
            "flow",
            "clients",
            "Published new-client counts summed over the requested months; not distinct people.",
        ),
        Metric.CLIENTES_BAJA: MetricDefinition(
            "m.clientes_baja",
            "flow",
            "clients",
            "Published client departures summed over the requested months; not a churn rate.",
        ),
        Metric.DECLARACIONES_RENTA: MetricDefinition(
            "m.declaraciones_renta",
            "flow",
            "declarations",
            "Published income-tax declaration counts summed over the requested months.",
        ),
        Metric.HORAS_TRABAJADAS: MetricDefinition(
            "m.horas_trabajadas",
            "flow",
            "hours",
            "Published worked hours summed over the requested months; "
            "missing hours remain unknown.",
        ),
    }
)


class PlanClarification(ValueError):
    """A valid intent cannot be executed without a business decision from the caller."""


class PlanUnsupported(ValueError):
    """The requested action is outside this deterministic analytics executor."""


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    parameters: tuple
    coverage_sql: str
    coverage_parameters: tuple
    row_limit: int


PUBLICATION_SQL = "SELECT id FROM ops.publications ORDER BY published_at DESC,id DESC LIMIT 1"
READER_SQL = """SELECT current_database() AS database_name,
    EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname IN ('core','mart','raw','ops') AND c.relkind IN ('r','p','v','m')
      AND has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE')) AS can_write"""

# A generated calendar exposes requested months beyond mart.coverage's existing
# publication range. The expected population is registered firms from fecha_alta;
# no unrecorded departure date or alternative definition of membership is inferred.
BASE_SQL = """WITH publication AS (
    SELECT id FROM ops.publications ORDER BY published_at DESC,id DESC LIMIT 1
), months AS (
    SELECT generate_series(%s::date,%s::date,interval '1 month')::date AS month
), firms AS (
    SELECT id,nombre,fecha_alta FROM core.firms
    WHERE (%s::int[] IS NULL OR id=ANY(%s::int[]))
      AND (%s::int IS NULL OR id=%s::int)
), observations AS (
    SELECT f.id AS firm_id,f.nombre AS firm_name,d.month,
      m.asesoria_id IS NOT NULL AS reported,
      m.has_rejected_newer_delivery AS fallback,
      {expression} AS value
    FROM months d CROSS JOIN firms f
    LEFT JOIN mart.firm_month m ON m.asesoria_id=f.id AND m.mes=d.month
      AND m.published_run_id=(SELECT id FROM publication)
    WHERE date_trunc('month',f.fecha_alta)::date<=d.month
)"""

COUNTS_SQL = """count(o.firm_id) AS expected_firm_months,
    count(o.value) AS observed_firm_months,
    count(*) FILTER (WHERE o.reported) AS reported_firm_months,
    count(*) FILTER (WHERE o.fallback) AS fallback_firm_months"""


def _scope(allowed_firm_ids: tuple[int, ...] | None) -> tuple[int, ...] | None:
    if allowed_firm_ids is None:
        return None
    if (
        not isinstance(allowed_firm_ids, tuple)
        or len(allowed_firm_ids) > MAX_SCOPE_FIRMS
        or any(type(firm) is not int or not 0 < firm <= 2_147_483_647 for firm in allowed_firm_ids)
    ):
        raise ValueError("Scope must be a tuple of at most 10000 positive PostgreSQL firm IDs")
    if not allowed_firm_ids:
        raise PermissionError("No firms are authorized")
    return tuple(sorted(set(allowed_firm_ids)))


def _validate_intent(plan: QueryPlan, scope: tuple[int, ...] | None) -> None:
    if plan.firm_id is not None and scope is not None and plan.firm_id not in scope:
        raise PermissionError("Requested firm is outside the authorized scope")
    if plan.action == "clarify":
        raise PlanClarification(plan.clarification or "Please specify the analytical question.")
    if plan.action == "investigate":
        raise PlanUnsupported(
            "Investigation requires its own tool; analytics does not infer causes."
        )
    if plan.action == "unsupported":
        raise PlanUnsupported("The requested action is unsupported by analytics.")
    if plan.metric is None or plan.operation is None:
        raise PlanClarification("Specify a metric and an operation.")
    if plan.start is None or plan.end is None:
        raise PlanClarification("Specify both start and end months (YYYY-MM), inclusive.")
    if (
        METRIC_CATALOGUE[plan.metric].kind == "stock"
        and plan.operation in (Operation.TOTAL, Operation.RANKING)
        and plan.start != plan.end
    ):
        raise PlanClarification("Active-client totals and rankings require a single month.")
    if plan.operation == Operation.DECLINE:
        if plan.metric != Metric.CLIENTES_ACTIVOS:
            raise PlanUnsupported("Decline is defined only for per-firm active-client stock.")
        if month_count(plan.start, plan.end) < plan.months_declining + 1:
            raise PlanClarification(
                f"{plan.months_declining} consecutive declines require at least "
                f"{plan.months_declining + 1} contiguous monthly observations."
            )


def compile_plan(plan: QueryPlan, allowed_firm_ids: tuple[int, ...] | None = None) -> CompiledQuery:
    """Pure compiler. Resolve a name first; returns bounded SQL and parameters.

    None scope is explicitly internal-network access; an empty tuple always denies.
    Ranking sorts observed firm totals descending, breaks ties by ID and excludes
    firms with no observations. Decline returns each matching rolling window.
    """
    plan = QueryPlan.model_validate(plan.model_dump())  # Reject model_construct/copy bypasses.
    scope = _scope(allowed_firm_ids)
    _validate_intent(plan, scope)
    if plan.firm_name is not None:
        raise PlanClarification("Resolve firm_name uniquely before compiling the analytical query.")
    scoped = list(scope) if scope is not None else None
    params = (
        date.fromisoformat(plan.start + "-01"),
        date.fromisoformat(plan.end + "-01"),
        scoped,
        scoped,
        plan.firm_id,
        plan.firm_id,
    )
    base = BASE_SQL.format(expression=METRIC_CATALOGUE[plan.metric].expression)
    coverage = (
        base
        + f"""
    SELECT d.month,{COUNTS_SQL}
    FROM months d LEFT JOIN observations o ON o.month=d.month
    GROUP BY d.month ORDER BY d.month LIMIT %s"""
    )
    if plan.operation == Operation.SERIES:
        sql = (
            base
            + f"""
        SELECT d.month,sum(o.value) AS value,{COUNTS_SQL}
        FROM months d LEFT JOIN observations o ON o.month=d.month
        GROUP BY d.month ORDER BY d.month LIMIT %s"""
        )
        query_params, limit = (*params, MAX_MONTHS + 1), MAX_MONTHS
    elif plan.operation == Operation.TOTAL:
        sql = base + f" SELECT sum(o.value) AS value,{COUNTS_SQL} FROM observations o LIMIT %s"
        query_params, limit = (*params, 1), 1
    elif plan.operation == Operation.RANKING:
        sql = (
            base
            + f"""
        SELECT o.firm_id,o.firm_name,sum(o.value) AS value,{COUNTS_SQL}
        FROM observations o GROUP BY o.firm_id,o.firm_name HAVING count(o.value)>0
        ORDER BY value DESC,o.firm_id LIMIT %s"""
        )
        query_params, limit = (*params, MAX_RESULTS + 1), MAX_RESULTS
    else:
        sql = (
            base
            + """, previous AS (
            SELECT *,lag(value) OVER w AS previous_value,lag(month) OVER w AS previous_month
            FROM observations WINDOW w AS (PARTITION BY firm_id ORDER BY month)
        ), comparisons AS (
            SELECT *,CASE WHEN value IS NOT NULL AND previous_value IS NOT NULL
              AND month=previous_month+interval '1 month' AND value<previous_value
              THEN 1 ELSE 0 END AS declined FROM previous
        ), windows AS (
            SELECT firm_id,firm_name,month AS end_month,value AS end_value,
              lag(month,%s) OVER w AS start_month,lag(value,%s) OVER w AS start_value,
              sum(declined) OVER (PARTITION BY firm_id ORDER BY month
                ROWS BETWEEN %s PRECEDING AND CURRENT ROW) AS declines
            FROM comparisons WINDOW w AS (PARTITION BY firm_id ORDER BY month)
        )
        SELECT firm_id,firm_name,start_month,end_month,start_value,end_value,
          end_value-start_value AS change,declines
        FROM windows WHERE declines=%s AND start_value IS NOT NULL
          AND end_month=start_month+(%s * interval '1 month')
        ORDER BY end_month DESC,change,firm_id LIMIT %s"""
        )
        n = plan.months_declining
        query_params, limit = (*params, n, n, n - 1, n, n, MAX_RESULTS + 1), MAX_RESULTS
    return CompiledQuery(sql, query_params, coverage, (*params, MAX_MONTHS), limit)


def compile_firm_lookup(
    plan: QueryPlan, allowed_firm_ids: tuple[int, ...] | None = None, *, exact: bool = True
) -> tuple[str, tuple]:
    """Literal, case-insensitive exact match first; bounded substring fallback.

    '!' is the explicit SQL escape character, so %, _ and ! in user names
    cannot broaden matching. Backslashes have no special meaning with this escape.
    """
    plan = QueryPlan.model_validate(plan.model_dump())
    scope = _scope(allowed_firm_ids)
    if plan.firm_id is not None and scope is not None and plan.firm_id not in scope:
        raise PermissionError("Requested firm is outside the authorized scope")
    if plan.firm_name is None:
        raise PlanClarification("Specify a firm name.")
    pattern = plan.firm_name.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    if not exact:
        pattern = "%" + pattern + "%"
    scoped = list(scope) if scope is not None else None
    return (
        """SELECT id AS firm_id,nombre AS firm_name FROM core.firms
        WHERE (%s::int[] IS NULL OR id=ANY(%s::int[]))
          AND (%s::int IS NULL OR id=%s::int) AND nombre ILIKE %s ESCAPE '!'
        ORDER BY lower(nombre),id LIMIT %s""",
        (scoped, scoped, plan.firm_id, plan.firm_id, pattern, MAX_NAME_MATCHES + 1),
    )


def _json(value):
    """Preserve numeric precision; never serialize Decimal through float."""
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(item) for item in value]
    return value


def _evidence(plan: QueryPlan) -> dict:
    definition = METRIC_CATALOGUE.get(plan.metric)
    return {
        "publication_id": None,
        "sql": None,
        "parameters": [],
        "columns": [],
        "rows": [],
        "coverage": None,
        "truncated": False,
        "period": {"start": plan.start, "end": plan.end, "inclusive": True},
        "metric_definition": None
        if definition is None
        else {
            "metric": plan.metric.value,
            "kind": definition.kind,
            "unit": definition.unit,
            "definition": definition.definition,
            "aggregation": "Sum of observed values only; missing values are not zero.",
            "decline": "Per firm, strict decreases across contiguous non-null monthly stocks; "
            "N declines require N+1 observations. All matching rolling windows are returned.",
        },
    }


def _response(status: str, answer: str, evidence: dict, **extra) -> dict:
    return {"status": status, "answer": answer, "evidence": _json(evidence), **_json(extra)}


def _coverage(rows: list[dict], query: CompiledQuery) -> dict:
    counts = {
        key: sum(row[key] for row in rows)
        for key in (
            "expected_firm_months",
            "observed_firm_months",
            "reported_firm_months",
            "fallback_firm_months",
        )
    }
    counts["missing_firm_months"] = counts["expected_firm_months"] - counts["reported_firm_months"]
    counts["null_metric_firm_months"] = (
        counts["reported_firm_months"] - counts["observed_firm_months"]
    )
    counts["complete"] = (
        counts["expected_firm_months"] > 0
        and counts["expected_firm_months"] == counts["observed_firm_months"]
    )
    return {
        **counts,
        "months": rows,
        "sql": query.coverage_sql,
        "parameters": query.coverage_parameters,
        "definition": "Registered firms from their fecha_alta month within the authorized "
        "scope, including requested months outside the publication range. "
        "Coverage describes the entire requested scope, before result truncation.",
    }


def _answer(plan: QueryPlan, rows: list[dict], coverage: dict, truncated: bool) -> str:
    period = f"{plan.start} through {plan.end} (inclusive)"
    observed, expected = coverage["observed_firm_months"], coverage["expected_firm_months"]
    if observed == 0:
        detail = "No observed metric values; absence is not zero."
    elif plan.operation == Operation.TOTAL:
        detail = f"Observed total: {rows[0]['value']} {METRIC_CATALOGUE[plan.metric].unit}."
    elif plan.operation == Operation.SERIES:
        detail = (
            "Monthly observed values: "
            + "; ".join(
                f"{row['month'].strftime('%Y-%m')}="
                f"{row['value'] if row['value'] is not None else 'unknown'}"
                for row in rows
            )
            + "."
        )
    elif plan.operation == Operation.RANKING:
        top = rows[0]
        detail = (
            f"{len(rows)} ranked firms returned. Highest observed total: "
            f"firm {top['firm_id']} ({top['firm_name']}), {top['value']} "
            f"{METRIC_CATALOGUE[plan.metric].unit}."
        )
    else:
        detail = (
            f"{len(rows)} windows of {plan.months_declining} consecutive per-firm "
            f"active-client declines returned; each requires "
            f"{plan.months_declining + 1} contiguous, non-null observations."
        )
        if rows:
            first = rows[0]
            detail += (
                f" First window: firm {first['firm_id']} ({first['firm_name']}), "
                f"{first['start_month'].isoformat()} to {first['end_month'].isoformat()}, "
                f"{first['start_value']} to {first['end_value']} clients "
                f"(change {first['change']})."
            )
    return (
        f"{plan.metric.value}, {period}. {detail} Coverage: {observed}/{expected} "
        "expected firm-months have a metric value. "
        + (
            "Partial coverage; results describe observed values only. "
            if not coverage["complete"]
            else ""
        )
        + ("Results truncated. " if truncated else "")
        + (
            f"{coverage['fallback_firm_months']} firm-months use an older accepted delivery."
            if coverage["fallback_firm_months"]
            else ""
        )
    ).strip()


def execute_plan(plan: QueryPlan, allowed_firm_ids: tuple[int, ...] | None = None) -> dict:
    """Execute with DATABASE_URL using a dedicated reader connection and snapshot.

    Production connections with table-write privileges are rejected. Only disposable
    databases named *_test may use the existing integration fixture's owner role.
    No relative date, alternative stock definition, or investigation is inferred.
    Validation errors and malformed scopes raise ValueError; access denials return
    unsupported without revealing other firms. SQL/transport failures expose no DSN.
    """
    plan = QueryPlan.model_validate(plan.model_dump())
    evidence = _evidence(plan)
    try:
        scope = _scope(allowed_firm_ids)
        _validate_intent(plan, scope)
    except PermissionError:
        return _response(
            "unsupported", "No access to the requested firm scope.", evidence, code="scope_denied"
        )
    except PlanClarification as exc:
        return _response("clarification", str(exc), evidence, code="clarification_required")
    except PlanUnsupported as exc:
        return _response("unsupported", str(exc), evidence, code="unsupported_plan")

    url = os.environ.get("DATABASE_URL")
    if not url:
        return _response(
            "unsupported",
            "Analytics database is unavailable.",
            evidence,
            code="database_unavailable",
        )
    try:
        with psycopg.connect(
            url,
            row_factory=dict_row,
            connect_timeout=5,
            options="-c default_transaction_read_only=on "
            "-c default_transaction_isolation=repeatable\\ read "
            "-c statement_timeout=5000 -c lock_timeout=1000 "
            "-c idle_in_transaction_session_timeout=10000",
        ) as conn:
            deadline = monotonic() + EXECUTION_TIMEOUT_SECONDS
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")

            def query(sql, parameters=()):
                remaining = int((deadline - monotonic()) * 1000)
                if remaining <= 0:
                    raise TimeoutError("Analytics execution budget exceeded")
                conn.execute(
                    "SELECT set_config('statement_timeout',%s,true)",
                    (str(min(STATEMENT_TIMEOUT_MS, remaining)),),
                )
                return conn.execute(sql, parameters)

            identity = query(READER_SQL).fetchone()
            if identity["can_write"] and not identity["database_name"].endswith("_test"):
                return _response(
                    "unsupported",
                    "Analytics requires a database reader role.",
                    evidence,
                    code="reader_required",
                )
            publication = query(PUBLICATION_SQL).fetchone()
            if publication is None:
                return _response(
                    "clarification",
                    "No recorded publication is available.",
                    evidence,
                    code="no_publication",
                )
            evidence["publication_id"] = publication["id"]
            if plan.firm_name is not None:
                lookups = []
                for exact in (True, False):
                    sql, parameters = compile_firm_lookup(plan, scope, exact=exact)
                    candidates = query(sql, parameters).fetchall()
                    lookups.append({"sql": sql, "parameters": parameters, "rows": candidates})
                    if candidates:
                        break
                evidence["firm_resolution"] = lookups
                if len(candidates) != 1:
                    evidence.update(
                        sql=sql,
                        parameters=parameters,
                        columns=["firm_id", "firm_name"],
                        rows=candidates[:MAX_NAME_MATCHES],
                        truncated=len(candidates) > MAX_NAME_MATCHES,
                    )
                    return _response(
                        "clarification",
                        "Choose a unique authorized firm by ID."
                        if candidates
                        else "No matching authorized firm was found; specify its name or ID.",
                        evidence,
                        code="ambiguous_firm" if candidates else "firm_not_found",
                        candidates=candidates[:MAX_NAME_MATCHES],
                    )
                plan = plan.model_copy(
                    update={"firm_id": candidates[0]["firm_id"], "firm_name": None}
                )
            compiled = compile_plan(plan, scope)
            evidence.update(sql=compiled.sql, parameters=compiled.parameters)
            cursor = query(compiled.sql, compiled.parameters)
            rows = cursor.fetchall()
            evidence["columns"] = [column.name for column in cursor.description]
            evidence["truncated"] = len(rows) > compiled.row_limit
            rows = rows[: compiled.row_limit]
            evidence["rows"] = rows
            coverage_rows = query(compiled.coverage_sql, compiled.coverage_parameters).fetchall()
            evidence["coverage"] = _coverage(coverage_rows, compiled)
            evidence["scope"] = {
                "kind": "internal_network" if scope is None else "authorized_firms",
                "firm_id": plan.firm_id,
            }
            return _response(
                "answered",
                _answer(plan, rows, evidence["coverage"], evidence["truncated"]),
                evidence,
            )
    except (TimeoutError, psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable):
        evidence.update(rows=[], columns=[], coverage=None, truncated=False)
        return _response(
            "unsupported",
            "Analytics exceeded its bounded execution budget.",
            evidence,
            code="query_timeout",
        )
    except psycopg.OperationalError:
        evidence.update(rows=[], columns=[], coverage=None, truncated=False)
        return _response(
            "unsupported",
            "Analytics database is unavailable.",
            evidence,
            code="database_unavailable",
        )
    except psycopg.Error:
        evidence.update(rows=[], columns=[], coverage=None, truncated=False)
        return _response(
            "unsupported",
            "The database could not execute this analytical query.",
            evidence,
            code="database_error",
        )
