"""Verification reports must not turn missing, crashed or failed tests green."""

import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "03_scripts"))
import test_dashboard as reports  # noqa: E402


def xml_file(tmp_path, body):
    path = tmp_path / "tests.xml"
    path.write_text(body, encoding="utf-8")
    return path


def test_nested_node_groups_are_counted_once_with_unique_case_names(tmp_path):
    path = xml_file(
        tmp_path,
        """<testsuites><testsuite name="transport failures" tests="2">
      <testcase name="HTTP" time="0.2" file="/repo/frontend/03_agents/tests/workbench.test.cjs"/>
      <testcase name="JSON" time="0.3" file="/repo/frontend/03_agents/tests/workbench.test.cjs">
        <failure message="bad payload">&lt;script&gt;fake&lt;/script&gt;</failure>
      </testcase></testsuite></testsuites>""",
    )
    cases = reports.parse_junit(path)
    assert reports.counts(cases) == {
        "total": 2,
        "passed": 1,
        "failed": 1,
        "errors": 0,
        "skipped": 0,
    }
    assert cases[0]["name"] == "transport failures / HTTP"
    assert cases[0]["module"] == "frontend/03_agents/tests/workbench.test.cjs"
    assert cases[0]["area"] == "Shared agent runtime (Phase 2)"
    assert "<script>" in cases[1]["message"]  # Escaping belongs to the DOM text renderer.


def test_pytest_failure_error_skip_and_pass_have_distinct_counts(tmp_path):
    path = xml_file(
        tmp_path,
        """<testsuites><testsuite name="pytest">
      <testcase name="test_insert[one]" classname="tests.test_ingestion"/>
      <testcase name="test_fail" classname="tests.test_ingestion">
        <failure message="mismatch"/></testcase>
      <testcase name="test_db" classname="tests.test_ingestion">
        <error message="DB offline"/></testcase>
      <testcase name="test_skip" classname="tests.test_ingestion">
        <skipped message="optional"/></testcase>
      </testsuite></testsuites>""",
    )
    suite = reports.blank_suite("backend")
    reports.complete_suite(suite, path, 1)
    assert suite["status"] == "failed"
    assert suite["counts"] == {"total": 4, "passed": 1, "failed": 1, "errors": 1, "skipped": 1}
    assert suite["cases"][0]["name"] == "test_insert[one]"


@pytest.mark.parametrize("xml", [None, "<broken", "<testsuites/>"])
def test_missing_invalid_or_empty_report_cannot_pass(tmp_path, xml):
    path = tmp_path / "absent.xml" if xml is None else xml_file(tmp_path, xml)
    suite = reports.blank_suite("backend")
    reports.complete_suite(suite, path, 0)
    assert suite["status"] == "error"
    assert suite["counts"]["passed"] == 0


def test_nonzero_exit_cannot_be_hidden_by_a_partial_passing_xml(tmp_path):
    path = xml_file(tmp_path, '<testsuites><testcase name="finished before crash"/></testsuites>')
    suite = reports.blank_suite("frontend")
    reports.complete_suite(suite, path, 137)
    assert suite["status"] == "failed"
    assert "137" in suite["error"]


def test_ci_collection_preserves_failed_exit_even_when_all_reported_cases_pass(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(reports, "OUTPUT", tmp_path)
    monkeypatch.setattr(reports, "git", lambda *args: "deadbeef" if args[0] == "rev-parse" else "")
    monkeypatch.setattr(sys, "argv", ["test_dashboard.py", "--collect"])
    for suite in ("backend", "frontend"):
        (tmp_path / f"{suite}.xml").write_text('<testsuites><testcase name="ok"/></testsuites>')
        (tmp_path / f"{suite}.started").write_text("2026-01-01T01:00:00Z")
        (tmp_path / f"{suite}.finished").write_text("2026-01-01T01:00:12Z")
    (tmp_path / "backend.exit").write_text("0")
    (tmp_path / "frontend.exit").write_text("137")
    assert reports.main() == 1
    result = json.loads((tmp_path / "report.json").read_text())
    assert result["status"] == "failed"
    assert result["started_at"] == "2026-01-01T01:00:00Z"
    assert result["suites"][0]["duration_seconds"] == 12
    assert result["suites"][0]["status"] == "passed"
    assert result["suites"][1]["status"] == "failed"


def test_startup_failure_replaces_old_green_result(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "OUTPUT", tmp_path)
    monkeypatch.setattr(reports, "git", lambda *args: "deadbeef" if args[0] == "rev-parse" else "")
    monkeypatch.setattr(sys, "argv", ["test_dashboard.py"])
    (tmp_path / "report.json").write_text('{"status":"passed"}')
    (tmp_path / "backend.xml").write_text('<testsuites><testcase name="old green"/></testsuites>')

    def fail_setup(report):
        raise RuntimeError("Docker unavailable")

    monkeypatch.setattr(reports, "run", fail_setup)
    assert reports.main() == 1
    result = json.loads((tmp_path / "report.json").read_text())
    assert result["status"] == "failed"
    assert all(s["status"] == "error" and s["counts"]["total"] == 0 for s in result["suites"])
    assert not (tmp_path / "backend.xml").exists()


def test_report_replacement_is_complete_and_runner_lock_rejects_overlap(tmp_path, monkeypatch):
    monkeypatch.setattr(reports, "OUTPUT", tmp_path)
    with reports.runner_lock():
        reports.publish({"status": "running", "suites": []})
        with pytest.raises(RuntimeError, match="already active"), reports.runner_lock():
            pass
        reports.publish({"status": "failed", "suites": []})
    assert json.loads((tmp_path / "report.json").read_text())["status"] == "failed"
    with reports.runner_lock():
        pass  # Released even after a competing lock acquisition failed.


def test_report_endpoint_is_read_only_uncached_and_only_exposes_fixed_json(tmp_path, monkeypatch):
    module = importlib.import_module("pulse_api.app")
    monkeypatch.setattr(module, "TEST_REPORT", tmp_path / "report.json")
    client = TestClient(module.app)
    assert client.get("/tests/report.json").status_code == 404
    (tmp_path / "report.json").write_text('{"status":"running"}')
    (tmp_path / "backend.log").write_text("private failure details")
    result = client.get("/tests/report.json")
    assert result.json() == {"status": "running"}
    assert result.headers["cache-control"] == "no-store"
    assert client.get("/tests/backend.log").status_code == 404
    assert client.post("/tests/report.json").status_code == 405
    assert client.get("/tests/").status_code == 200
    assert client.get("/tests/app.js").status_code == 200
