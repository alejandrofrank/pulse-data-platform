"""CI-only fault injection: restart Compose with a quarantined CSV, retaining volumes."""

import argparse
import json
import os
import subprocess
import urllib.request
from pathlib import Path


def get(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=15) as response:
        assert response.status == 200
        return json.load(response)


def compose(*args):
    subprocess.run(["docker", "compose", *args], check=True, timeout=240)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable", action="store_true")
    args = parser.parse_args()
    if not args.disposable or os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("Only run with --disposable in the disposable GitHub Actions stack")
    drops = (Path.cwd() / "data" / "drops").resolve(strict=True)
    path = drops / "metricas_2026-04__999.csv"
    assert not path.exists(), "Never overwrite an existing delivery"
    network = get("/api/v1/network/metrics?limit=500")
    before = get("/api/v1/quality/summary")
    content = b"broken,header\n1,2\n"
    # Retain all volumes, including the previously verified lab. The default up
    # command must pass the real bootstrap dependency gate, not an HTTP-only mock.
    compose("--profile", "lab", "down")
    path.write_bytes(content)
    try:
        compose("up", "-d", "--wait")
        assert get("/health")["status"] == "ok"
        after = get("/api/v1/quality/summary")
        assert after["latest_run"]["status"] == "partial", after
        assert after["latest_run"]["files_failed"] == 1, after
        for key in ("files", "raw_rows", "accepted_rows", "rejected_rows", "curated_rows"):
            assert after[key] == before[key], key
        assert get("/api/v1/network/metrics?limit=500") == network
        errors = get("/api/v1/quality/file-errors?limit=500")["items"]
        assert any(row["filename"] == path.name and row["resolved_at"] is None for row in errors)
        run_id = after["latest_run"]["dagster_run_id"]
        request = urllib.request.Request(
            "http://127.0.0.1:3000/graphql",
            data=json.dumps(
                {
                    "query": "query($id:ID!){pipelineRunOrError(runId:$id)"
                    "{__typename ... on Run {status}}}",
                    "variables": {"id": run_id},
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            run = json.load(response)["data"]["pipelineRunOrError"]
        assert run == {"__typename": "Run", "status": "FAILURE"}, run
        print(
            json.dumps(
                {
                    "result": "passed",
                    "diagnostics_available": True,
                    "business_totals_unchanged": True,
                    "quality_status": "partial",
                    "dagster_status": "FAILURE",
                }
            )
        )
    finally:
        # Delete only the exact temporary file this script created, never sources
        # or volumes. Its database error evidence survives until CI teardown.
        if path.is_file() and path.read_bytes() == content:
            path.unlink()


if __name__ == "__main__":
    main()
