"""Pure acceptance-evaluator regressions: no database, provider, or real HTTP calls."""

import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "03_scripts"))

import evaluate_agents as evaluation  # noqa: E402

EXPECTED = next(case[3] for case in evaluation.CASES if case[0] == "decline")
PUBLICATION = "602a9fbc-d485-4cbc-80d6-a3dbab1d63b7"
MONTHS = [f"2025-{month:02d}-01" for month in range(1, 5)]


def window(firm_id=1, start_value=100, end_value=70):
    return {
        "firm_id": firm_id,
        "firm_name": f"Firm {firm_id}",
        "start_month": MONTHS[0],
        "end_month": MONTHS[-1],
        "start_value": start_value,
        "end_value": end_value,
        "change": end_value - start_value,
        "declines": 3,
    }


def answer(rows=None, truncated=False):
    return {
        "status": "answered",
        "plan": {**EXPECTED, "firm_id": None, "firm_name": None},
        "evidence": {
            "scope": {"kind": "internal_network", "firm_id": None},
            "period": {"start": "2025-01", "end": "2025-04", "inclusive": True},
            "publication_id": PUBLICATION,
            "rows": [window()] if rows is None else rows,
            "truncated": truncated,
        },
    }


class ReportingAPI:
    """Independent small reporting fixture, including missing and plateau stocks."""

    def __init__(self, stocks=None):
        self.stocks = stocks or {
            1: [100, 90, 80, 70],
            2: [100, 90, "absent", 70],
            3: [100, 90, 90, 70],
            4: [100, 110, 80, 70],
            5: [100, None, 80, 70],
        }
        self.firms = [{"id": firm_id, "nombre": f"Firm {firm_id}"} for firm_id in self.stocks]
        self.calls = []

    def __call__(self, base, path, timeout):
        assert 0 < timeout <= 5
        assert base == "http://reporting.invalid"
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        offset = int(query["offset"][0])
        assert query["limit"] == ["500"]
        self.calls.append(path)
        if parsed.path == "/api/v1/firms":
            items = self.firms
        else:
            assert query["start"] == ["2025-01"] and query["end"] == ["2025-04"]
            firm_id = int(parsed.path.split("/")[-2])
            assert parsed.path == f"/api/v1/firms/{firm_id}/metrics"
            items = [
                {
                    "asesoria_id": firm_id,
                    "mes": month,
                    "clientes_activos": stock,
                    "coverage_status": "missing" if stock is None else "available",
                }
                for month, stock in zip(MONTHS, self.stocks[firm_id], strict=True)
                if stock != "absent"
            ]
        return {
            "items": copy.deepcopy(items[offset : offset + 500]),
            "total": len(items),
            "limit": 500,
            "offset": offset,
        }, PUBLICATION


class DeclineEvaluationTests(unittest.TestCase):
    def verify(self, result, api=None):
        return evaluation.verify_decline(
            result, EXPECTED, "http://reporting.invalid", get=api or ReportingAPI()
        )

    def test_correct_result_checks_entire_network_with_readonly_reporting_paths(self):
        api = ReportingAPI()
        report = self.verify(answer(), api)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["reference"]["firms_checked"], 5)
        self.assertEqual(report["reference"]["matching_windows"], 1)
        self.assertEqual(report["reference"]["requests"], 7)
        self.assertEqual(len(report["reference"]["reference_sha256"]), 64)
        self.assertTrue(all(path.startswith("/api/v1/firms") for path in api.calls))

    def test_empty_missing_extra_duplicate_and_corrupt_rows_fail(self):
        cases = [[], [window(), window()], [window(999)], [{"firm_id": -999, "change": 123456789}]]
        for field, value in {
            "firm_id": True,
            "firm_name": "Invented name",
            "start_month": "2025-02-01",
            "end_month": "2025-05-01",
            "start_value": 101,
            "end_value": 69,
            "change": -999,
            "declines": 2,
        }.items():
            cases.append([{**window(), field: value}])
        for rows in cases:
            with self.subTest(rows=rows):
                self.assertEqual(self.verify(answer(rows))["status"], "failed")

    def test_missing_month_plateau_increase_and_unknown_stock_cannot_qualify(self):
        for firm_id in (2, 3, 4, 5):
            with self.subTest(firm=firm_id):
                report = self.verify(answer([window(), window(firm_id)]))
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["reference"]["rows"], [window()])

    def test_zero_windows_is_valid_only_when_independent_reference_is_empty(self):
        api = ReportingAPI({1: [100, 90, 90, 70]})
        self.assertEqual(self.verify(answer([]), api)["status"], "passed")
        self.assertEqual(self.verify(answer(), api)["status"], "failed")

    def test_every_window_is_compared_in_deterministic_order(self):
        api = ReportingAPI({1: [100, 90, 80, 70], 2: [100, 80, 70, 50]})
        rows = [window(2, 100, 50), window()]
        self.assertEqual(self.verify(answer(rows), api)["status"], "passed")
        self.assertEqual(self.verify(answer(rows[:1]), api)["status"], "failed")
        self.assertEqual(self.verify(answer(rows[::-1]), api)["status"], "failed")

    def test_calendar_windows_overlap_and_cross_year_boundary(self):
        firms = [{"id": 1, "nombre": "Firm 1"}]
        observations = {1: {"2024-11-01": 10, "2024-12-01": 9, "2025-01-01": 8, "2025-02-01": 7}}
        rows = evaluation.decline_windows(firms, observations, "2024-11", "2025-02", 2)
        self.assertEqual([row["end_month"] for row in rows], ["2025-02-01", "2025-01-01"])
        self.assertEqual([row["change"] for row in rows], [-2, -2])

    def test_truncation_requires_exact_top_100_and_explicit_flag(self):
        api = ReportingAPI({firm: [100, 90, 80, 70] for firm in range(1, 102)})
        rows = [window(firm) for firm in range(1, 101)]
        report = self.verify(answer(rows, True), api)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["reference"]["matching_windows"], 101)
        for result in (
            answer(rows),
            answer(rows[:99], True),
            answer(rows[1:] + [window(101)], True),
        ):
            self.assertEqual(self.verify(result, api)["status"], "failed")
        self.assertEqual(self.verify(answer(truncated=True))["status"], "failed")

    def test_directory_pagination_checks_firms_beyond_first_page(self):
        api = ReportingAPI({firm: [100, 100, 100, 100] for firm in range(1, 502)})
        api.stocks[501] = [100, 90, 80, 70]
        report = self.verify(answer([window(501)]), api)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["reference"]["firms_checked"], 501)
        self.assertIn("/api/v1/firms?limit=500&offset=500", api.calls)

    def test_wrong_plan_scope_period_and_shape_fail_before_reference_gets(self):
        cases = []
        for field, value in (("firm_id", 1), ("firm_name", "Firm 1"), ("months_declining", 2)):
            result = answer()
            result["plan"][field] = value
            cases.append(result)
        for field, value in (
            ("scope", {"kind": "authorized_firms", "firm_id": None}),
            ("period", {"start": "2024-01", "end": "2024-04", "inclusive": True}),
        ):
            result = answer()
            result["evidence"][field] = value
            cases.append(result)
        cases.append({"status": "answered", "plan": [], "evidence": ["invalid"]})
        get = Mock(side_effect=AssertionError("Wrong scope must not fetch a reference"))
        for result in cases:
            self.assertEqual(self.verify(result, get)["status"], "failed")
        get.assert_not_called()

    def test_different_saved_publication_never_passes_even_when_current_values_match(self):
        result = answer()
        result["evidence"]["publication_id"] = "older-publication"
        report = self.verify(result)
        self.assertEqual(report["status"], "unverifiable")
        self.assertTrue(report["current_rows_match"])

    def test_changed_or_missing_publication_never_mixes_snapshots(self):
        for when, publication in ((2, "new-publication"), (7, "new-publication"), (1, None)):
            api = ReportingAPI()

            def get(base, path, timeout, api=api, when=when, publication=publication):
                page, observed = api(base, path, timeout)
                return page, publication if len(api.calls) == when else observed

            with self.subTest(when=when, publication=publication):
                self.assertEqual(self.verify(answer(), get)["status"], "unverifiable")

    def test_incomplete_or_invalid_reference_is_unverifiable(self):
        for mutation in ("short_page", "oversized_directory", "duplicate_month", "wrong_firm"):
            api = ReportingAPI()

            def get(base, path, timeout, api=api, mutation=mutation):
                page, publication = api(base, path, timeout)
                if mutation == "short_page":
                    page["items"] = []
                elif mutation == "oversized_directory":
                    page["total"] = 1001
                elif "/metrics?" in path:
                    if mutation == "duplicate_month":
                        page["items"][1]["mes"] = page["items"][0]["mes"]
                    else:
                        page["items"][0]["asesoria_id"] = 999
                return page, publication

            with self.subTest(mutation=mutation):
                self.assertEqual(self.verify(answer(), get)["status"], "unverifiable")

    def test_timeout_and_collection_deadline_do_not_pass(self):
        with patch.object(evaluation, "monotonic", side_effect=[0, 121]):
            self.assertEqual(self.verify(answer())["status"], "unverifiable")
        get = Mock(side_effect=TimeoutError("Reporting timed out"))
        self.assertEqual(self.verify(answer(), get)["status"], "unverifiable")

    def test_saved_cli_uses_only_gets_and_does_not_trust_saved_pass_or_expected_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "saved.json"
            output = Path(directory) / "new" / "nested" / "recheck.json"
            payload = {
                "results": [
                    {"case": "decline", "passed": True, "expected": {}, "result": answer([])}
                ]
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            original = path.read_bytes()
            with (
                patch.object(
                    sys,
                    "argv",
                    [
                        "evaluate_agents.py",
                        "--recheck-saved",
                        str(path),
                        "--base",
                        "http://reporting.invalid",
                        "--output",
                        str(output),
                    ],
                ),
                patch.object(evaluation, "reporting_get", ReportingAPI()),
                patch.object(evaluation, "request", side_effect=AssertionError("No agent calls")),
                patch("builtins.print"),
                self.assertRaises(SystemExit) as exit_code,
            ):
                evaluation.main()
            self.assertEqual(exit_code.exception.code, 1)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "failed")

    def test_reporting_transport_forces_get_timeout_and_bounded_body(self):
        for payload in (b'{"items":[]}', b"x" * (evaluation.MAX_REFERENCE_BYTES + 1)):
            reply = io.BytesIO(payload)
            reply.headers = {"X-Pulse-Publication-ID": PUBLICATION}
            opener = Mock()
            opener.open.return_value = reply
            with patch.object(evaluation, "build_opener", return_value=opener):
                if len(payload) > evaluation.MAX_REFERENCE_BYTES:
                    with self.assertRaises(evaluation.ReferenceUnavailable):
                        evaluation.reporting_get("http://reporting.invalid", "/api/v1/firms", 3)
                else:
                    _, publication = evaluation.reporting_get(
                        "http://reporting.invalid", "/api/v1/firms", 3
                    )
                    self.assertEqual(publication, PUBLICATION)
            req = opener.open.call_args.args[0]
            self.assertEqual(req.get_method(), "GET")
            self.assertIsNone(req.data)
            self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)


if __name__ == "__main__":
    unittest.main()
