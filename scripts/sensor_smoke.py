"""Prove automatic pickup through the real daemon, ONLY on a disposable Compose stack."""

import argparse
import json
import time
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--base", default="http://127.0.0.1:8000")
parser.add_argument("--dagster", default="http://127.0.0.1:3000")
parser.add_argument("--data-dir", type=Path, required=True)
parser.add_argument("--disposable", action="store_true", required=True)
args = parser.parse_args()


def call(url, body=None):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def api(path):
    return call(args.base + "/api/v1/" + path)


def dagster_run(run_id):
    result = call(
        args.dagster + "/graphql",
        {
            "query": """query($id:ID!){runOrError(runId:$id){__typename
            ... on Run {status tags{key value} runConfigYaml}}}""",
            "variables": {"id": run_id},
        },
    )
    assert not result.get("errors"), result
    return result["data"]["runOrError"]


before = api("network/metrics?limit=500")
assert api("quality/summary")["files"] == 36, "Requires the pristine 36-delivery test fixture"
source = args.data_dir.resolve() / "drops" / "metricas_2026-03.csv"
destination = source.with_name("metricas_2026-03__001.csv")
temporary = source.with_name("metricas_2026-03__001.partial")
assert source.is_file() and not destination.exists() and not temporary.exists()
try:
    temporary.write_bytes(source.read_bytes())
    temporary.replace(destination)
    deadline = time.monotonic() + 180
    run = None
    while time.monotonic() < deadline:
        deliveries = api("quality/deliveries?limit=100")["items"]
        delivery = next((d for d in deliveries if d["filename"] == destination.name), None)
        if delivery:
            runs = api("quality/runs?limit=100")["items"]
            run = next(r for r in runs if r["id"] == delivery["run_id"])
            if run["status"] == "success":
                dagster = dagster_run(run["dagster_run_id"])
                if dagster.get("status") == "SUCCESS":
                    break
        time.sleep(2)
    else:
        raise AssertionError(f"Sensor did not automatically ingest the new revision: {run}")
    assert run["files_loaded"] == 1 and run["files_skipped"] == 0, run
    assert {t["key"]: t["value"] for t in dagster["tags"]}["pulse/trigger"] == "file_sensor"
    assert destination.name in dagster["runConfigYaml"]
    assert api("network/metrics?limit=500") == before, "Identical revision changed business totals"
    counts = api("quality/summary")
    runs_before = api("quality/runs?limit=1")["total"]
    # Let at least two more normal sensor evaluations observe the already archived file.
    deadline = time.monotonic() + 70
    while time.monotonic() < deadline:
        time.sleep(2)
    assert api("quality/runs?limit=1")["total"] == runs_before, (
        "Sensor repeatedly re-ingested an unchanged file"
    )
    after = api("quality/summary")
    assert all(after[k] == counts[k] for k in ("files", "raw_rows", "curated_rows"))
    print(
        json.dumps(
            {
                "result": "passed",
                "trigger": "file_sensor",
                "files_loaded": 1,
                "dagster_run_id": run["dagster_run_id"],
                "unchanged_recheck": True,
            }
        )
    )
finally:
    # Only the two test-created filenames are removed. Database evidence remains until
    # the caller tears down this explicitly disposable Compose project.
    destination.unlink(missing_ok=True)
    temporary.unlink(missing_ok=True)
