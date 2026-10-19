"""Run the two test suites and atomically publish inspectable, key-free results.

Run from the repository root with Docker, Python and Node 24+ installed. The API
only reads report.json; it cannot launch commands or access the Docker socket.
"""

import argparse
import json
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "test-results" / "dashboard"


def now():
    return datetime.now(UTC).isoformat()


def area(module, name):
    key = (module + " " + name).lower()
    if "test_dashboard" in module or "04_tests" in module:
        return "Test tooling"
    if "investigation" in key or "investigator" in key:
        return "Data investigator (Phase 2)"
    if "agent_analytics" in key or "business" in name.lower():
        return "Business agent (Phase 2)"
    if "agent" in key or "workbench" in key:
        return "Shared agent runtime (Phase 2)"
    if "lab" in key:
        return "Playback lab"
    if any(word in key for word in ("orchestration", "operations", "sensor")):
        return "Orchestration & operations (Phase 1)"
    if "api" in key or "dashboard" in key:
        return "API & reporting (Phase 1)"
    return "Pipeline & quality (Phase 1)"


def parse_junit(path):
    """Count leaf cases once, preserving nested Node test names and pytest IDs."""
    root = ET.parse(path).getroot()
    cases = []

    def visit(element, parents):
        if element.tag == "testcase":
            module = element.get("file") or element.get("classname", "unknown")
            module = module.replace("\\", "/")
            for prefix in ("/frontend/", "/backend/"):
                if prefix in module:
                    module = prefix.strip("/") + "/" + module.split(prefix, 1)[1]
            name = " / ".join([*parents, element.get("name", "Unnamed case")])
            status, message = "passed", ""
            for tag, outcome in (("error", "error"), ("failure", "failed"), ("skipped", "skipped")):
                detail = element.find(tag)
                if detail is not None:
                    status = outcome
                    message = (detail.get("message", "") + "\n" + (detail.text or "")).strip()
                    break
            cases.append(
                {
                    "name": name,
                    "module": module,
                    "area": area(module, name),
                    "status": status,
                    "duration_seconds": float(element.get("time", 0)),
                    "message": message[:12000],
                }
            )
            return
        # pytest's single suite is a framework container, not a test name.
        if element.tag == "testsuite" and element.get("name") not in (None, "pytest"):
            parents = [*parents, element.get("name")]
        for child in element:
            visit(child, parents)

    visit(root, [])
    if not cases:
        raise ValueError("The report contains no test cases")
    return cases


def counts(cases):
    values = Counter(case["status"] for case in cases)
    return {
        "total": len(cases),
        "passed": values["passed"],
        "failed": values["failed"],
        "skipped": values["skipped"],
        "errors": values["error"],
    }


def blank_suite(name):
    return {
        "id": name,
        "label": name.title(),
        "status": "pending",
        "started_at": None,
        "finished_at": None,
        "duration_seconds": None,
        "exit_code": None,
        "error": None,
        "counts": counts([]),
        "cases": [],
    }


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def new_report(ci=False):
    ci_url = None
    if ci and os.environ.get("GITHUB_RUN_ID"):
        ci_url = (
            f"https://github.com/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    return {
        "schema_version": 1,
        "run_id": str(uuid4()),
        "status": "running",
        "started_at": now(),
        "finished_at": None,
        "updated_at": now(),
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
        "source": "ci" if ci else "local",
        "ci_url": ci_url,
        "suites": [blank_suite("backend"), blank_suite("frontend")],
    }


def publish(report):
    report["updated_at"] = now()
    temp = OUTPUT / "report.tmp"
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, OUTPUT / "report.json")


@contextmanager
def runner_lock():
    """OS-held lock releases even after a crash; two dashboard runs cannot race."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with (OUTPUT / ".runner.lock").open("a+b") as handle:
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("A dashboard test run is already active") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def command(args, log, report, cwd=ROOT, timeout=900):
    print("Running:", " ".join(str(arg) for arg in args), flush=True)
    with (OUTPUT / log).open("wb") as output:
        process = subprocess.Popen(args, cwd=cwd, stdout=output, stderr=subprocess.STDOUT)
        started = time.monotonic()
        try:
            while process.poll() is None:
                if time.monotonic() - started > timeout:
                    raise TimeoutError(f"{log}: exceeded {timeout}s; inspect the log")
                publish(report)
                time.sleep(2)
            return process.returncode
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def complete_suite(suite, path, exit_code):
    suite["finished_at"] = now()
    suite["exit_code"] = exit_code
    try:
        suite["cases"] = parse_junit(path)
        suite["counts"] = counts(suite["cases"])
        failed = suite["counts"]["failed"] + suite["counts"]["errors"]
        suite["status"] = "failed" if failed or exit_code else "passed"
        if exit_code and not failed:
            suite["error"] = f"Runner exited with code {exit_code}; inspect the suite log."
    except (OSError, ValueError, ET.ParseError) as exc:
        suite["status"] = "error"
        suite["error"] = f"Test report unavailable or invalid: {exc}"


def run(report):
    node_version = subprocess.check_output(["node", "--version"], text=True).strip()
    if int(node_version.removeprefix("v").split(".")[0]) < 24:
        raise RuntimeError("Node 24+ is required for source filenames in the test dashboard")
    # Build the current checkout; never silently test an older packaged image.
    for args, log in (
        (["docker", "compose", "build", "test"], "build.log"),
        (["docker", "compose", "up", "-d", "--wait", "db"], "database.log"),
    ):
        if command(args, log, report):
            raise RuntimeError(f"Preparation failed; see {OUTPUT / log}")
    frontend = ROOT / "frontend"
    if not (frontend / "node_modules" / "jsdom").exists():
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm or command([npm, "ci"], "npm-install.log", report, cwd=frontend):
            raise RuntimeError("Install frontend dependencies with npm --prefix frontend ci")
    container = "pulse-tests-" + report["run_id"]
    # The container's unprivileged uid must be able to create the report on Linux.
    # This directory holds test artifacts only; no credentials or application data.
    if os.name != "nt":
        OUTPUT.chmod(0o777)
    backend_command = [
        "docker",
        "compose",
        "run",
        "--rm",
        "--name",
        container,
        "-v",
        f"{OUTPUT.as_posix()}:/reports",
        "test",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--junitxml=/reports/backend.xml",
    ]
    frontend_command = [
        "node",
        "--test",
        "--test-reporter=junit",
        f"--test-reporter-destination={OUTPUT / 'frontend.xml'}",
        *[str(path.relative_to(frontend)) for path in sorted(frontend.glob("*/tests/*.test.cjs"))],
    ]
    for suite, args, cwd in (
        (report["suites"][0], backend_command, ROOT),
        (report["suites"][1], frontend_command, frontend),
    ):
        suite.update(status="running", started_at=now())
        publish(report)
        start = time.monotonic()
        try:
            code = command(args, f"{suite['id']}.log", report, cwd=cwd)
            complete_suite(suite, OUTPUT / f"{suite['id']}.xml", code)
        except (OSError, TimeoutError) as exc:
            suite.update(status="error", error=str(exc), finished_at=now())
        finally:
            if suite["id"] == "backend":
                # If interrupted, the Docker client exiting need not stop the container.
                subprocess.run(
                    ["docker", "rm", "--force", container],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                )
        suite["duration_seconds"] = round(time.monotonic() - start, 3)
        publish(report)
        print(suite["id"], suite["status"], suite["counts"], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--collect", action="store_true", help="Collect CI-generated JUnit files only"
    )
    args = parser.parse_args()
    with runner_lock():
        report = new_report(ci=args.collect)
        if not args.collect:
            for name in ("backend.xml", "frontend.xml"):
                (OUTPUT / name).unlink(missing_ok=True)
        publish(report)
        try:
            if args.collect:
                for suite in report["suites"]:
                    path = OUTPUT / f"{suite['id']}.xml"
                    code_path = OUTPUT / f"{suite['id']}.exit"
                    code = int(code_path.read_text().strip()) if code_path.exists() else 1
                    complete_suite(suite, path, code)
                    for field, suffix in (("started_at", "started"), ("finished_at", "finished")):
                        timing = OUTPUT / f"{suite['id']}.{suffix}"
                        suite[field] = timing.read_text().strip() if timing.exists() else None
                    if suite["started_at"] and suite["finished_at"]:
                        suite["duration_seconds"] = (
                            datetime.fromisoformat(suite["finished_at"])
                            - datetime.fromisoformat(suite["started_at"])
                        ).total_seconds()
                starts = [s["started_at"] for s in report["suites"] if s["started_at"]]
                if starts:
                    report["started_at"] = min(starts)
            else:
                run(report)
            report["status"] = (
                "passed" if all(s["status"] == "passed" for s in report["suites"]) else "failed"
            )
        except (Exception, KeyboardInterrupt) as exc:
            report["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            for suite in report["suites"]:
                if suite["status"] in ("pending", "running"):
                    suite.update(
                        status="error", error=str(exc) or "Run interrupted", finished_at=now()
                    )
        finally:
            report["finished_at"] = now()
            publish(report)
            # CI artifact can be served with `python -m http.server` after downloading.
            for source in (ROOT / "frontend" / "04_tests" / "public").glob("*"):
                if source.is_file():
                    shutil.copyfile(source, OUTPUT / source.name)
        print(f"{report['status']}: http://127.0.0.1:8000/tests/", flush=True)
        return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
