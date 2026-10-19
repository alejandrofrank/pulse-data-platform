"""Bounded, deterministic revenue reconciliation. No model or arbitrary SQL execution."""

import hashlib
import json
import os
import threading
import time
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
from uuid import uuid4

from pulse.db import connect

REVENUE = (
    "facturacion_asesoria_eur",
    "facturacion_gestion_eur",
    "facturacion_consultoria_eur",
    "facturacion_otros_eur",
)
MAX_VERSIONS = 50
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_ARCHIVE_BYTES = 8 * 1024 * 1024
SLOTS = threading.BoundedSemaphore(2)
KEY_FILTER = """(r.normalized->>'asesoria_id'=%s AND r.normalized->>'mes'=%s)
    OR (ltrim(btrim(r.payload->>'asesoria_id'),'0')=%s AND btrim(r.payload->>'mes')=%s)"""


class InvestigationMissing(ValueError):
    pass


class InvestigationBusy(RuntimeError):
    pass


def canonical(value):
    return json.loads(json.dumps(value, default=str))


def money(values):
    try:
        parts = [Decimal(str(values[name])) for name in REVENUE]
        return sum(parts, Decimal(0)) if all(x.is_finite() for x in parts) else None
    except (KeyError, TypeError, InvalidOperation):
        return None


def equivalent(left, right):
    """Compare JSON numeric values semantically, never via binary floating point."""
    if left is None or right is None:
        return left is right
    try:
        a, b = Decimal(str(left)), Decimal(str(right))
        return a.is_finite() and b.is_finite() and a == b
    except InvalidOperation:
        return str(left) == str(right)


def finding(code, status, detail, **evidence):
    return {"code": code, "status": status, "detail": detail, **evidence}


def _collect(firm_id, month):
    from pulse_api.reconciliation import VERIFIER_VERSION, VerificationLimit, verify_delivery

    checks, queries = [], []
    deadline = time.monotonic() + 15
    with connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout='5s'")
        conn.execute("SET LOCAL lock_timeout='1s'")
        identity = conn.execute("""SELECT current_database() AS db,
            has_table_privilege(current_user,'mart.firm_month','UPDATE') AS writer""").fetchone()
        if identity["writer"] and not identity["db"].endswith("_test"):
            raise PermissionError("Investigation requires a database reader role")

        def query(sql, params=(), *, one=False):
            if time.monotonic() > deadline:
                raise InvestigationBusy("Investigation evidence deadline exceeded")
            queries.append({"sql": sql, "parameters": canonical(params)})
            cursor = conn.execute(sql, params)
            return cursor.fetchone() if one else cursor.fetchall()

        firm = query("SELECT * FROM core.firms WHERE id=%s", (firm_id,), one=True)
        if not firm:
            raise InvestigationMissing("Firm not found")
        publication = query(
            "SELECT * FROM ops.publications ORDER BY published_at DESC LIMIT 1", one=True
        )
        publication_id = str(publication["id"]) if publication else None
        published = query(
            "SELECT * FROM mart.firm_month WHERE asesoria_id=%s AND mes=%s",
            (firm_id, month),
            one=True,
        )
        selected_id = published["source_record_id"] if published else None
        key = (str(firm_id), str(month), str(firm_id), month.strftime("%Y-%m"))
        total = query(f"SELECT count(*) AS n FROM raw.records r WHERE {KEY_FILTER}", key, one=True)[
            "n"
        ]
        select = """SELECT r.id,r.delivery_id,r.line_number,r.status,
            CASE WHEN octet_length(r.payload::text)<=16384 THEN r.payload
                 ELSE '{}'::jsonb END AS payload,
            CASE WHEN octet_length(r.normalized::text)<=16384 THEN r.normalized
                 ELSE '{}'::jsonb END AS normalized,
            (octet_length(r.payload::text)>16384 OR octet_length(r.normalized::text)>16384)
                AS oversized_record,
            d.filename,d.delivery_month,d.revision,d.sha256,
            d.loaded_at,d.schema_version,d.policy_version,d.run_id,
            run.status AS ingestion_status,
            EXISTS(SELECT 1 FROM ops.publication_deliveries pd
                WHERE pd.delivery_id=d.id AND pd.publication_id=%s) AS published_input,
            coalesce((SELECT jsonb_agg(to_jsonb(i) ORDER BY i.id)
                FROM (SELECT id,code,severity,field,left(message,1000) AS message
                      FROM ops.issues WHERE record_id=r.id ORDER BY id LIMIT 20) i),'[]') AS issues
            FROM raw.records r JOIN raw.deliveries d ON d.id=r.delivery_id
            JOIN ops.runs run ON run.id=d.run_id"""
        versions = query(
            select + f" WHERE {KEY_FILTER} "
            "ORDER BY d.delivery_month DESC,d.revision DESC,r.line_number LIMIT %s",
            (publication_id, *key, MAX_VERSIONS),
        )
        expected = query(
            select
            + """ WHERE r.normalized->>'asesoria_id'=%s
            AND r.normalized->>'mes'=%s AND r.status='accepted'
            AND EXISTS(SELECT 1 FROM ops.publication_deliveries pd
                WHERE pd.delivery_id=d.id AND pd.publication_id=%s)
            ORDER BY d.delivery_month DESC,d.revision DESC,r.line_number LIMIT 1""",
            (publication_id, str(firm_id), str(month), publication_id),
            one=True,
        )
        selected = (
            query(select + " WHERE r.id=%s", (publication_id, selected_id), one=True)
            if selected_id is not None
            else None
        )
        expected_id = expected["id"] if expected else None
        checks.append(
            finding(
                "winner_selection",
                "match"
                if publication and selected_id == expected_id
                else "mismatch"
                if publication
                else "unavailable",
                "Latest accepted record within the publication manifest; recorded eligibility "
                "is independently checked below for the bounded evidence set.",
                expected_record_id=expected_id,
                selected_record_id=selected_id,
            )
        )
        if published and publication and str(published["published_run_id"]) != publication_id:
            checks.append(
                finding(
                    "publication_identity",
                    "mismatch",
                    "Published row belongs to a different publication.",
                )
            )
        if not publication:
            checks.append(
                finding(
                    "publication_manifest",
                    "unavailable",
                    "No recorded publication manifest. Run ingestion after migration.",
                )
            )
        if total > len(versions):
            checks.append(
                finding(
                    "history_complete",
                    "unavailable",
                    "History exceeds the bounded investigation window; paginate lineage.",
                )
            )
        if any(not row["published_input"] for row in versions):
            checks.append(
                finding(
                    "unpublished_deliveries",
                    "unavailable",
                    "Some source attempts are outside this publication; check run status.",
                )
            )
        # Never infer absence from normalized keys alone. Whole-file failures cannot
        # reliably be attributed to one firm and are reported as a separate scope.
        file_errors = query("""SELECT id,filename,sha256,reason,run_id,first_seen_at,last_seen_at
            FROM ops.file_errors WHERE resolved_at IS NULL
            ORDER BY last_seen_at DESC,id DESC LIMIT 21""")
        error_count = query(
            "SELECT count(*) AS n FROM ops.file_errors WHERE resolved_at IS NULL", one=True
        )["n"]
        if error_count:
            checks.append(
                finding(
                    "unattributed_file_errors",
                    "unavailable",
                    "Quarantined files exist; their contents are not safely attributable "
                    "to this firm-month. No claim of complete source coverage is made.",
                )
            )

        # Verify complete candidate deliveries so duplicate eligibility is evaluated
        # across a whole delivery, not just the selected row. Enforce byte budgets.
        candidates = {r["id"]: r for r in versions}
        for row in (selected, expected):
            if row:
                candidates[row["id"]] = row
        if any(row["oversized_record"] for row in candidates.values()):
            checks.append(
                finding("record_budget", "unavailable", "Oversized record fields omitted.")
            )
        firms = {r["id"]: r for r in query("SELECT * FROM core.firms ORDER BY id LIMIT 100001")}
        verified, consumed = {}, 0
        for delivery_id in dict.fromkeys(r["delivery_id"] for r in candidates.values()):
            archive = query(
                """SELECT filename,sha256,octet_length(content) AS size,
                policy_version FROM raw.deliveries WHERE id=%s""",
                (delivery_id,),
                one=True,
            )
            if (
                archive["size"] > MAX_ARCHIVE_BYTES
                or consumed + archive["size"] > MAX_TOTAL_ARCHIVE_BYTES
                or len(firms) > 100000
                or archive["policy_version"] != "1"
            ):
                checks.append(
                    finding(
                        "archive_verification",
                        "unavailable",
                        "Archive exceeds verification budget or uses an unsupported policy.",
                        delivery_id=delivery_id,
                    )
                )
                continue
            content = bytes(
                query("SELECT content FROM raw.deliveries WHERE id=%s", (delivery_id,), one=True)[
                    "content"
                ]
            )
            consumed += len(content)
            digest = hashlib.sha256(content).hexdigest()
            checks.append(
                finding(
                    "archive_checksum",
                    "match" if digest == archive["sha256"] else "mismatch",
                    "Archived bytes compared with the registered checksum.",
                    delivery_id=delivery_id,
                    sha256=digest,
                )
            )
            try:
                result = verify_delivery(content, archive["filename"], firms, max_records=5000)
                wanted = {
                    r["line_number"] for r in candidates.values() if r["delivery_id"] == delivery_id
                }
                # Do not retain every record of every archive (large object expansion).
                verified[delivery_id] = {
                    "records": {
                        line: record for line, record in result["records"].items() if line in wanted
                    }
                }
                del result
            except VerificationLimit:
                checks.append(
                    finding(
                        "archive_verification",
                        "unavailable",
                        "Archive exceeds the independent record-count budget.",
                        delivery_id=delivery_id,
                    )
                )
            except ValueError:
                checks.append(
                    finding(
                        "archive_verification",
                        "mismatch",
                        "Independent verifier cannot read the registered archive.",
                        delivery_id=delivery_id,
                    )
                )

        original_revenue = None
        for row in candidates.values():
            oracle = verified.get(row["delivery_id"], {}).get("records", {}).get(row["line_number"])
            if not oracle:
                checks.append(
                    finding(
                        "source_payload",
                        "mismatch" if row["delivery_id"] in verified else "unavailable",
                        "Original CSV record could not be verified.",
                        record_id=row["id"],
                    )
                )
                continue
            matches_payload = canonical(oracle["payload"]) == canonical(row["payload"])
            checks.append(
                finding(
                    "source_payload",
                    "match" if matches_payload else "mismatch",
                    "Archived CSV record compared with the stored raw payload.",
                    record_id=row["id"],
                    filename=row["filename"],
                    ending_physical_line=row["line_number"],
                )
            )
            left, right = canonical(oracle["normalized"]), row["normalized"]
            differing = [
                k
                for k in left.keys() | right.keys()
                if k not in left or k not in right or not equivalent(left.get(k), right.get(k))
            ]
            checks.append(
                finding(
                    "revenue_normalization",
                    "mismatch" if any(k in REVENUE for k in differing) else "match",
                    "Independent Decimal parsing of the original revenue fields.",
                    record_id=row["id"],
                    differing_fields=differing,
                )
            )
            checks.append(
                finding(
                    "row_validation",
                    "match" if not differing and oracle["status"] == row["status"] else "mismatch",
                    "Independent full-row normalization and duplicate eligibility.",
                    record_id=row["id"],
                    expected_status=oracle["status"],
                    recorded_status=row["status"],
                    differing_fields=differing,
                )
            )
            if row["id"] == expected_id:
                original_revenue = money(oracle["normalized"])

        published_revenue = money(published) if published else None
        expected_revenue = money(expected["normalized"]) if expected else None
        components_match = (published is None and expected is None) or bool(
            published
            and expected
            and all(equivalent(published.get(k), expected["normalized"].get(k)) for k in REVENUE)
        )
        checks.append(
            finding(
                "mart_revenue",
                "match"
                if publication and components_match
                else "mismatch"
                if publication
                else "unavailable",
                "All four published revenue components compared with the selected input.",
                expected=expected_revenue,
                actual=published_revenue,
            )
        )
        if not expected:
            checks.append(
                finding(
                    "no_accepted_source",
                    "unavailable",
                    "No accepted source record in this publication; absence is not zero.",
                )
            )
        evidence = {
            "publication_id": publication_id,
            "publication": publication,
            "selected_record_id": selected_id,
            "expected_record_id": expected_id,
            "selected_source": selected,
            "expected_source": expected,
            "versions": {"items": versions, "total": total, "truncated": total > len(versions)},
            "unattributed_file_errors": {
                "items": file_errors[:20],
                "total": error_count,
                "truncated": error_count > 20,
            },
            "published": published,
            "published_revenue_eur": published_revenue,
            "original_revenue_eur": original_revenue,
            "api_revenue_eur": None,
            "verifier_version": VERIFIER_VERSION,
            "sql": queries,
            "limitations": [
                "Upstream business correctness requires independent supplier evidence.",
                "Historical HTTP responses and dashboard rendering are not reconstructed.",
                "Checksums do not protect against privileged rewrites of both bytes and hash.",
                "Unidentifiable corrupt rows and quarantined files are not exhaustively searched.",
            ],
        }
    return checks, evidence


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_metric(firm_id, month):
    """Only call the colocated reporting service; no user-controlled URL or redirects."""
    port = int(os.environ.get("PULSE_INTERNAL_API_PORT", "8000"))
    if port not in (8000, 8001):
        raise ValueError("Unsupported internal API port")
    url = f"http://127.0.0.1:{port}/api/v1/firms/{firm_id}/metrics?start={month}&end={month}"
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(url, timeout=5) as result:
            body = result.read(256 * 1024 + 1)
            if len(body) > 256 * 1024:
                raise ValueError("API evidence exceeds byte limit")
            return {
                "publication_id": result.headers.get("X-Pulse-Publication-ID"),
                "status_code": result.status,
                "body": json.loads(body),
                "url": url,
            }
    except HTTPError as exc:
        return {"publication_id": None, "status_code": exc.code, "body": {}, "url": url}


def investigate(firm_id: int, month: date, fetch_api=None):
    if firm_id <= 0 or month.day != 1:
        raise ValueError("Use a positive firm ID and the first day of a month")
    if not SLOTS.acquire(blocking=False):
        raise InvestigationBusy("Two investigations are already running; retry shortly")
    try:
        for attempt in range(2):
            checks, evidence = _collect(firm_id, month)
            try:
                observation = (fetch_api or fetch_metric)(firm_id, month.strftime("%Y-%m"))
            except (OSError, ValueError, HTTPException):
                observation = {"status_code": None, "publication_id": None, "body": {}}
            observation["observed_at"] = datetime.now(UTC).isoformat()
            evidence["api_observation"] = observation
            if observation["status_code"] != 200:
                checks.append(
                    finding("api_revenue", "unavailable", "Reporting API could not be read.")
                )
                break
            if (
                not evidence["publication_id"]
                or observation["publication_id"] != evidence["publication_id"]
            ):
                if attempt == 0:
                    continue
                checks.append(
                    finding(
                        "publication_changed",
                        "unavailable",
                        "API and database publications differ; comparison withheld.",
                    )
                )
                break
            body = observation.get("body")
            items = body.get("items") if isinstance(body, dict) else None
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                checks.append(finding("api_revenue", "mismatch", "Malformed metrics API response."))
                break
            item = items[0] if len(items) == 1 else None
            evidence["api_revenue_eur"] = item.get("revenue_eur") if item else None
            published = evidence["published"]
            if published:
                matches = bool(
                    item
                    and item.get("asesoria_id") == firm_id
                    and item.get("mes") == str(month)
                    and item.get("source_record_id") == evidence["selected_record_id"]
                    and item.get("coverage_status") == "available"
                    and item.get("has_rejected_newer_delivery")
                    == published["has_rejected_newer_delivery"]
                    and all(isinstance(item.get(k), str) for k in (*REVENUE, "revenue_eur"))
                    and all(equivalent(item.get(k), published.get(k)) for k in REVENUE)
                    and equivalent(item.get("revenue_eur"), evidence["published_revenue_eur"])
                )
            else:
                matches = not items or bool(
                    item
                    and item.get("asesoria_id") == firm_id
                    and item.get("mes") == str(month)
                    and item.get("source_record_id") is None
                    and all(item.get(k) is None for k in REVENUE)
                    and item.get("revenue_eur") is None
                    and item.get("coverage_status") == "missing"
                )
            checks.append(
                finding(
                    "api_revenue",
                    "match" if matches else "mismatch",
                    "HTTP JSON identity, components and total checked at the same publication.",
                )
            )
            break
        status = (
            "discrepancy"
            if any(c["status"] == "mismatch" for c in checks)
            else (
                "inconclusive"
                if any(c["status"] == "unavailable" for c in checks)
                else "consistent"
            )
        )
        stable = {"findings": checks, "evidence": evidence}
        digest = hashlib.sha256(json.dumps(canonical(stable), sort_keys=True).encode()).hexdigest()
        return canonical(
            {
                "investigation_id": str(uuid4()),
                "firm_id": firm_id,
                "month": month,
                "metric": "revenue_eur",
                "status": status,
                "created_at": datetime.now(UTC),
                "evidence_sha256": digest,
                "findings": checks,
                "evidence": evidence,
            }
        )
    finally:
        SLOTS.release()
