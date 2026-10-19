"""Pure investigation regressions: in-memory evidence, no database or HTTP access.

The connection double implements the bounded evidence-query protocol, not SQL.
PostgreSQL query semantics remain covered by the parent's serial integration suite.
"""

import copy
import csv
import hashlib
import io
import json
import unittest
from datetime import date
from unittest.mock import patch

from pulse_api import investigation, reconciliation

MONTH = date(2025, 10, 1)
HEADER = (
    "asesoria_id,mes,clientes_activos,clientes_nuevos,clientes_baja,declaraciones_renta,"
    "declaraciones_iva,declaraciones_sociedades,declaraciones_otros,facturacion_asesoria_eur,"
    "facturacion_gestion_eur,facturacion_consultoria_eur,horas_trabajadas,consultas_recibidas,"
    "consultas_resueltas,incidencias_aeat,documentos_procesados,satisfaccion_cliente"
).split(",")


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return copy.deepcopy(self.rows)

    def fetchone(self):
        return copy.deepcopy(self.rows[0]) if self.rows else None


class Snapshot:
    def __init__(self):
        self.firm = {"id": 1, "nombre": "Synthetic firm", "fecha_alta": date(2020, 1, 1)}
        self.rows = []
        self.archives = {}
        self.selected_id = None
        self.in_coverage = True
        self.closed = False
        self.queries = []

    def add(self, filename="metricas_2025-10.csv", amount="100", **changes):
        payload = dict.fromkeys(HEADER, "10")
        payload.update(
            asesoria_id="1",
            mes="2025-10",
            satisfaccion_cliente="4",
            facturacion_asesoria_eur=amount,
        )
        payload.update(changes.pop("payload", {}))
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=HEADER)
        writer.writeheader()
        writer.writerow(payload)
        content = output.getvalue().encode()
        oracle = reconciliation.verify_delivery(content, filename, {1: self.firm})["records"][2]
        delivery_month, revision = investigation.delivery_order(filename)
        ident = len(self.rows) + 1
        row = {
            "id": ident,
            "delivery_id": ident,
            "line_number": 2,
            "status": oracle["status"],
            "payload": oracle["payload"],
            "normalized": investigation.canonical(oracle["normalized"]),
            "filename": filename,
            "delivery_month": delivery_month,
            "revision": revision,
            "sha256": hashlib.sha256(content).hexdigest(),
            "policy_version": "1",
            "published_input": True,
            **changes,
        }
        self.rows.append(row)
        self.archives[ident] = content
        return row

    @property
    def published(self):
        row = next((r for r in self.rows if r["id"] == self.selected_id), None)
        if row is None:
            return None
        # Unknown JSON keys do not become mart columns.
        normalized = {k: v for k, v in row["normalized"].items() if k != "padding"}
        return {
            **normalized,
            "source_record_id": row["id"],
            "published_run_id": "publication-1",
            "has_rejected_newer_delivery": False,
        }

    def project(self, row):
        row = copy.deepcopy(row)
        for field in ("payload", "normalized"):
            omitted = len(json.dumps(row[field]).encode()) > 16384
            row[field + "_omitted"] = omitted
            if omitted:
                row[field] = {}
        row["oversized_record"] = row["payload_omitted"] or row["normalized_omitted"]
        return row

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        if sql.startswith("SET "):
            return Cursor([])
        if "AS writer" in sql:
            return Cursor([{"db": "pulse", "writer": False}])
        if sql.startswith("SELECT * FROM core.firms"):
            return Cursor([self.firm])
        if sql.startswith("SELECT * FROM ops.publications"):
            return Cursor([{"id": "publication-1"}])
        if sql.startswith("SELECT * FROM mart.firm_month"):
            return Cursor([self.published] if self.published else [])
        if sql.startswith("SELECT asesoria_id,mes,status FROM mart.coverage"):
            assert params == (1, MONTH)
            return Cursor(
                [
                    {
                        "asesoria_id": 1,
                        "mes": MONTH,
                        "status": "available" if self.published else "missing",
                    }
                ]
                if self.in_coverage
                else []
            )
        if sql.startswith("SELECT count(*) AS n FROM raw.records"):
            return Cursor([{"n": len(self.rows)}])
        if sql.startswith("SELECT r.id,r.delivery_id"):
            if "WHERE r.id=%s" in sql:
                return Cursor([self.project(r) for r in self.rows if r["id"] == params[-1]])
            rows = sorted(
                self.rows,
                key=lambda r: (r["delivery_month"], r["revision"], -r["line_number"]),
                reverse=True,
            )
            if "r.status='accepted'" in sql:
                assert params[-1] == investigation.MAX_VERSIONS + 1
                rows = [r for r in rows if r["status"] == "accepted" and r["published_input"]]
                fields = (
                    "id",
                    "delivery_id",
                    "line_number",
                    "filename",
                    "delivery_month",
                    "revision",
                )
                return Cursor([{k: r[k] for k in fields} for r in rows[: params[-1]]])
            assert params[-1] == investigation.MAX_VERSIONS
            return Cursor([self.project(r) for r in rows[: params[-1]]])
        if "FROM ops.file_errors" in sql:
            return Cursor([{"n": 0}] if "count(*)" in sql else [])
        if sql.startswith("SELECT filename,sha256,octet_length(content)"):
            row = next(r for r in self.rows if r["delivery_id"] == params[0])
            return Cursor([{**row, "size": len(self.archives[params[0]])}])
        if sql.startswith("SELECT content FROM raw.deliveries"):
            return Cursor([{"content": self.archives[params[0]]}])
        raise AssertionError(f"Unexpected evidence query: {sql}")

    def fetch(self, firm_id, month):
        assert self.closed, "Database snapshot must close before HTTP observation"
        assert (firm_id, month) == (1, "2025-10")
        items = []
        if self.in_coverage:
            if self.published:
                item = {
                    **self.published,
                    "coverage_status": "available",
                    "revenue_eur": str(investigation.money(self.published)),
                }
            else:
                item = {
                    **dict.fromkeys(investigation.REVENUE),
                    "asesoria_id": 1,
                    "mes": str(MONTH),
                    "source_record_id": None,
                    "coverage_status": "missing",
                    "revenue_eur": None,
                }
            items.append(item)
        return {
            "publication_id": "publication-1",
            "status_code": 200,
            "body": {"items": items, "total": len(items), "limit": 100, "offset": 0},
        }


class InvestigationAuditTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = Snapshot()
        self.enterContext(patch.object(investigation, "connect", return_value=self.snapshot))
        self.enterContext(
            patch.object(investigation, "fetch_metric", side_effect=AssertionError("No HTTP"))
        )

    def run_report(self, alter_api=None):
        def fetch(*args):
            observation = self.snapshot.fetch(*args)
            if alter_api:
                alter_api(observation["body"])
            return observation

        return investigation.investigate(1, MONTH, fetch)

    def assert_check(self, report, code, status):
        states = {f["status"] for f in report["findings"] if f["code"] == code}
        self.assertIn(status, states, report["findings"])

    def assert_no_mismatch(self, report):
        self.assertNotIn("mismatch", [f["status"] for f in report["findings"]])

    def test_omitted_payload_is_unavailable_not_corruption(self):
        row = self.snapshot.add(amount=" " * 17000 + "100")
        self.assertEqual(row["status"], "accepted")
        self.snapshot.selected_id = row["id"]
        report = self.run_report()
        self.assertEqual(report["status"], "inconclusive")
        self.assert_check(report, "source_payload", "unavailable")
        for code in ("revenue_normalization", "row_validation", "mart_revenue", "api_revenue"):
            self.assert_check(report, code, "match")
        self.assert_no_mismatch(report)

    def test_omitted_normalization_does_not_compare_empty_placeholder(self):
        row = self.snapshot.add()
        row["normalized"]["padding"] = "x" * 17000
        self.snapshot.selected_id = row["id"]
        report = self.run_report()
        self.assertEqual(report["status"], "inconclusive")
        for code in ("revenue_normalization", "row_validation", "mart_revenue"):
            self.assert_check(report, code, "unavailable")
        self.assert_check(report, "source_payload", "match")
        self.assertEqual(report["evidence"]["original_revenue_eur"], "120.00")
        self.assert_no_mismatch(report)

    def test_omitted_payload_does_not_hide_independent_numeric_disagreement(self):
        row = self.snapshot.add(amount=" " * 17000 + "100")
        row["normalized"]["facturacion_asesoria_eur"] = "101"
        self.snapshot.selected_id = row["id"]
        report = self.run_report()
        self.assertEqual(report["status"], "discrepancy")
        self.assert_check(report, "source_payload", "unavailable")
        self.assert_check(report, "revenue_normalization", "mismatch")

    def test_filename_revision_selects_winner_despite_swapped_metadata(self):
        older = self.snapshot.add(revision=1)
        newer = self.snapshot.add("metricas_2025-10__001.csv", "200", revision=0)
        self.snapshot.selected_id = older["id"]
        report = self.run_report()
        self.assertEqual(report["status"], "discrepancy")
        for code in ("delivery_order", "winner_selection", "mart_revenue"):
            self.assert_check(report, code, "mismatch")
        self.assertEqual(report["evidence"]["expected_record_id"], newer["id"])
        self.assertEqual(report["evidence"]["original_revenue_eur"], "220.00")
        self.assert_check(report, "api_revenue", "match")

    def test_filename_month_selects_winner_despite_swapped_metadata(self):
        older = self.snapshot.add(delivery_month=date(2025, 11, 1))
        newer = self.snapshot.add("metricas_2025-11.csv", "200", delivery_month=MONTH)
        self.snapshot.selected_id = older["id"]
        report = self.run_report()
        self.assertEqual(report["evidence"]["expected_record_id"], newer["id"])
        self.assertEqual(report["evidence"]["original_revenue_eur"], "220.00")
        self.assert_check(report, "delivery_order", "mismatch")
        self.assert_check(report, "winner_selection", "mismatch")

    def test_correct_revision_order_remains_consistent(self):
        self.snapshot.add()
        newer = self.snapshot.add("metricas_2025-10__001.csv", "200")
        self.snapshot.selected_id = newer["id"]
        report = self.run_report()
        self.assertEqual(report["status"], "consistent")
        self.assertEqual(report["evidence"]["original_revenue_eur"], "220.00")
        self.assert_check(report, "winner_selection", "match")

    def test_truncated_accepted_candidates_withhold_selection(self):
        self.snapshot.add()
        newer = self.snapshot.add("metricas_2025-10__001.csv", "200")
        self.snapshot.selected_id = newer["id"]
        with patch.object(investigation, "MAX_VERSIONS", 1):
            report = self.run_report()
        self.assertEqual(report["status"], "inconclusive")
        self.assertIsNone(report["evidence"]["expected_record_id"])
        self.assert_check(report, "winner_selection", "unavailable")
        self.assert_check(report, "mart_revenue", "unavailable")
        self.assertNotIn("no_accepted_source", [f["code"] for f in report["findings"]])
        self.assert_no_mismatch(report)

    def test_truncated_rejected_history_still_checks_pinned_accepted_winner(self):
        accepted = self.snapshot.add()
        self.snapshot.add("metricas_2025-10__001.csv", payload={"satisfaccion_cliente": "99"})
        self.snapshot.selected_id = accepted["id"]
        with patch.object(investigation, "MAX_VERSIONS", 1):
            report = self.run_report()
        self.assertEqual(report["status"], "inconclusive")
        self.assertEqual(report["evidence"]["expected_record_id"], accepted["id"])
        self.assert_check(report, "winner_selection", "match")
        self.assert_check(report, "mart_revenue", "match")
        self.assert_no_mismatch(report)

    def test_archive_byte_limit_is_inconclusive(self):
        self.snapshot.selected_id = self.snapshot.add()["id"]
        with patch.object(investigation, "MAX_ARCHIVE_BYTES", 1):
            report = self.run_report()
        self.assertEqual(report["status"], "inconclusive")
        self.assert_check(report, "archive_verification", "unavailable")
        self.assert_no_mismatch(report)

    def test_archive_record_limit_is_inconclusive(self):
        self.snapshot.selected_id = self.snapshot.add()["id"]
        with patch.object(
            reconciliation, "verify_delivery", side_effect=reconciliation.VerificationLimit("limit")
        ):
            report = self.run_report()
        self.assertEqual(report["status"], "inconclusive")
        self.assert_check(report, "archive_verification", "unavailable")
        self.assert_no_mismatch(report)

    def test_missing_marker_omission_is_detected_even_with_zero_total(self):
        self.snapshot.add(payload={"satisfaccion_cliente": "99"})
        for total in (0, 1):
            with self.subTest(total=total):
                report = self.run_report(
                    lambda body, total=total: body.update(items=[], total=total)
                )
                self.assertEqual(report["status"], "discrepancy")
                self.assert_check(report, "api_revenue", "mismatch")
                self.assertEqual(report["evidence"]["expected_coverage"]["status"], "missing")

    def test_missing_marker_with_consistent_total_matches(self):
        self.snapshot.add(payload={"satisfaccion_cliente": "99"})
        report = self.run_report()
        self.assertEqual(report["status"], "inconclusive")
        self.assert_check(report, "api_revenue", "match")
        self.assert_no_mismatch(report)

    def test_empty_response_outside_coverage_matches(self):
        self.snapshot.in_coverage = False
        report = self.run_report()
        self.assertEqual(report["status"], "inconclusive")
        self.assertIsNone(report["evidence"]["expected_coverage"])
        self.assert_check(report, "api_revenue", "match")
        self.assert_no_mismatch(report)

    def test_empty_response_outside_coverage_rejects_nonzero_total(self):
        self.snapshot.in_coverage = False
        report = self.run_report(lambda body: body.update(total=1))
        self.assert_check(report, "api_revenue", "mismatch")

    def test_unexpected_item_outside_coverage_is_detected(self):
        self.snapshot.in_coverage = False
        report = self.run_report(lambda body: body.update(items=[{}], total=1))
        self.assert_check(report, "api_revenue", "mismatch")

    def test_available_revenue_requires_correct_integer_total(self):
        self.snapshot.selected_id = self.snapshot.add()["id"]
        for total in (None, True, "1", 0, 2, -1):
            with self.subTest(total=total):
                report = self.run_report(lambda body, total=total: body.update(total=total))
                self.assertEqual(report["status"], "discrepancy")
                self.assert_check(report, "api_revenue", "mismatch")

    def test_duplicate_api_items_are_detected(self):
        self.snapshot.selected_id = self.snapshot.add()["id"]
        report = self.run_report(lambda body: body["items"].append(copy.deepcopy(body["items"][0])))
        self.assert_check(report, "api_revenue", "mismatch")

    def test_coverage_query_is_parameterized_and_returned_with_evidence(self):
        report = self.run_report()
        queries = [q for q in report["evidence"]["sql"] if "FROM mart.coverage" in q["sql"]]
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]["parameters"], [1, "2025-10-01"])
        self.assertTrue(self.snapshot.closed)

    def test_filename_contract_has_independent_strict_calendar_and_revision(self):
        self.assertEqual(investigation.delivery_order("metricas_2025-10.csv"), (MONTH, 0))
        self.assertEqual(investigation.delivery_order("metricas_2025-10__999.csv"), (MONTH, 999))
        for filename in (
            "metricas_2025-00.csv",
            "metricas_0000-10.csv",
            "metricas_2025-10__1.csv",
            "metricas_2025-10.csv.extra",
        ):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                investigation.delivery_order(filename)


if __name__ == "__main__":
    unittest.main()
