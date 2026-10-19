"""Two bounded agent workflows sharing deterministic evidence tools."""

import hashlib
import os
import time
from datetime import date, datetime
from functools import lru_cache
from importlib.resources import files
from zoneinfo import ZoneInfo

from pulse_agents import provider, store
from pulse_agents.analytics import execute_plan
from pulse_agents.schemas import QueryPlan

EXAMPLES = [
    {"agent": "business", "question": "Network revenue in March 2026"},
    {"agent": "business", "question": "Active clients by month in 2025"},
    {"agent": "investigation", "question": "Investigate revenue for firm 42 in January 2024"},
]

CLARIFICATIONS = {
    "missing_metric": "Which metric should I query: revenue, clients, declarations or hours?",
    "missing_period": "Which month or inclusive date range should I use?",
    "missing_firm": "Which firm ID should I investigate, and for which month?",
    "ambiguous_clients": "Do you mean fewer active clients, or the number of client departures?",
    "ambiguous_revenue": "Do you mean billed revenue? Profit and cash receipts are unavailable.",
    "unsupported_history": "Past screen states are unavailable. I can investigate the current "
    "published value and its recorded delivery history.",
}


@lru_cache(maxsize=1)
def implementation_version():
    digest = hashlib.sha256()
    for package in ("pulse_agents", "pulse_api"):
        for path in sorted(files(package).iterdir(), key=lambda item: item.name):
            if path.name.endswith(".py"):
                digest.update(f"{package}/{path.name}\n".encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()


def scope():
    configured = os.environ.get("PULSE_AGENT_ALLOWED_FIRMS", "").strip()
    if not configured:
        return None
    values = tuple(sorted({int(part.strip()) for part in configured.split(",")}))
    if len(values) > 5000 or any(value <= 0 for value in values):
        raise ValueError("Invalid configured firm scope")
    return values


def status():
    return {
        **provider.provider_status(),
        "profiles": ["business", "investigation"],
        "examples": EXAMPLES,
        "deployment": "internal operator",
        "max_requests_per_hour": 60,
        "max_concurrent_runs": 4,
        "daily_budget_usd": os.environ.get("PULSE_AGENT_DAILY_BUDGET_USD", "0.50"),
    }


def investigation_answer(report):
    bad = sorted({item["code"] for item in report["findings"] if item["status"] == "mismatch"})
    missing = sorted(
        {item["code"] for item in report["findings"] if item["status"] == "unavailable"}
    )
    if bad:
        return "Discrepancies found in: " + ", ".join(bad) + ". Inspect the evidence below."
    if missing:
        return (
            "The available checks do not establish a complete reconciliation. "
            "Evidence missing or limited: " + ", ".join(missing) + "."
        )
    return (
        "The archived source, recorded policy, published revenue and reporting API agree. "
        "This verifies Pulse's handling; supplier business correctness needs upstream evidence."
    )


def ask(
    request_id,
    conversation_id,
    profile,
    question,
    *,
    owner="local-operator",
    allowed_firm_ids=None,
    can_investigate=True,
):
    started = time.monotonic()
    current = provider.provider_status()
    configured_scope = scope()
    authorized = allowed_firm_ids
    if configured_scope is not None:
        authorized = (
            configured_scope
            if authorized is None
            else tuple(sorted(set(authorized) & set(configured_scope)))
        )
    admission = store.begin(
        request_id,
        conversation_id,
        profile,
        question,
        owner,
        live=current["mode"] == "live",
        authorization={"firms": authorized, "investigate": can_investigate},
    )
    if "cached" in admission:
        return admission["cached"]
    conversation_id = admission["conversation_id"]
    state = admission["state"]
    result = {
        "request_id": str(request_id),
        "conversation_id": conversation_id,
        "agent": profile,
        "status": "error",
        "answer": "Request could not be completed",
        "plan": None,
        "evidence": None,
        "trace": {
            "implementation_sha256": implementation_version(),
            "prompt_version": "pulse-intent-v1",
            "run_id": str(request_id),
            "model": current["model"],
            "mode": current["mode"],
            "input_tokens": None,
            "output_tokens": None,
            "cost_usd": None,
            "replayed": False,
        },
    }
    try:
        context = {
            "execution_mode": current["mode"],
            "previous_plan": state.get("last_plan"),
            "recent_questions": state.get("recent_questions", [])[-2:],
            "current_date": datetime.now(ZoneInfo("Europe/Madrid")).date().isoformat(),
        }
        interpreted = provider.plan(question, profile, context)
        plan = QueryPlan.model_validate(interpreted["plan"])
        result["plan"] = plan.model_dump(mode="json")
        result["trace"].update(interpreted["trace"])
        if plan.action == "clarify":
            result.update(
                status="clarification",
                answer=CLARIFICATIONS.get(
                    plan.clarification,
                    (
                        "Please specify the metric, firm or network, and month or date range. "
                        "Investigations need one firm and one month. "
                        "Historical screen states are unavailable."
                    ),
                ),
            )
        elif plan.action == "unsupported":
            result.update(
                status="unsupported",
                answer=("This request is outside the read-only metrics and investigation tools."),
            )
        elif profile == "business" and plan.action == "query":
            analytical = execute_plan(plan, authorized)
            result.update(
                status=analytical["status"],
                answer=analytical["answer"],
                evidence=analytical.get("evidence"),
            )
        elif profile == "investigation" and plan.action == "investigate":
            if authorized is not None or not can_investigate:
                # Forensic evidence includes shared delivery/file errors and can
                # reveal other firms. Never treat scoped analytical permission as
                # permission to inspect platform-wide operational evidence.
                result.update(
                    status="unsupported",
                    answer=(
                        "Investigation requires the internal operator's full operational access."
                    ),
                )
            elif (
                plan.firm_id is None
                or plan.firm_name is not None
                or not plan.start
                or plan.end != plan.start
                or plan.metric != "revenue_eur"
            ):
                result.update(
                    status="clarification",
                    answer=(
                        "Specify a firm ID and one YYYY-MM period for total-revenue investigation."
                    ),
                )
            else:
                from pulse_api.investigation import investigate

                report = investigate(plan.firm_id, date.fromisoformat(plan.start + "-01"))
                result.update(
                    status="answered", answer=investigation_answer(report), evidence=report
                )
        else:
            result.update(
                status="clarification",
                answer=(
                    "Use Business assistant for queries and Data investigator for reconciliation."
                ),
            )
        # Preserve intent, not answer values. Facts are retrieved again on every turn.
        state = {
            "last_plan": result["plan"],
            "recent_questions": [*state.get("recent_questions", [])[-1:], question],
        }
    except Exception as exc:
        # No provider or database exception text is returned: messages may contain
        # credentials, SQL internals or remote content. Audit a safe class only.
        result["trace"]["error_type"] = type(exc).__name__
        if isinstance(exc, provider.ProviderUnavailable):
            result["trace"]["error_code"] = exc.code
        result.update(
            status="error",
            answer=(
                "The request could not be verified. No data was changed. "
                "Check provider configuration or retry with a new request identifier."
            ),
        )
    result["trace"]["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    return store.finish(request_id, result, state)
