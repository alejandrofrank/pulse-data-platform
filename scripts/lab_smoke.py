"""End-to-end verification ONLY against an explicitly disposable, initially empty lab."""

import argparse
import json
import time
import urllib.request
from uuid import uuid4

parser = argparse.ArgumentParser()
parser.add_argument("--base", default="http://127.0.0.1:8001")
parser.add_argument("--disposable", action="store_true", required=True)
args = parser.parse_args()


def call(path, body=None):
    request = urllib.request.Request(
        args.base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Pulse-Lab": "playback-v1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def play(files):
    job_id = str(uuid4())
    body = {"request_id": job_id, "filenames": files, "interval_ms": 0}
    assert call("/api/lab/play", body)["id"] == job_id
    # A lost-response retry must return the same run, not enqueue another.
    assert call("/api/lab/play", body)["id"] == job_id
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = call("/api/lab/state")
        job = state["job"]
        assert state["worker_alive"] and not state["worker_error"]
        if job["id"] == job_id and job["status"] in ("success", "failed"):
            assert job["status"] == "success", job
            return state
        time.sleep(0.4)
    raise AssertionError("Playback timed out")


state = call("/api/lab/state")
assert state["counts"]["files"] == 0 and not state["jobs"], (
    "Refusing to reset an existing experiment"
)
files = [f["filename"] for f in state["catalog"]]
assert len(files) == 36
first = play(files)
assert first["counts"]["published_rows"] == 17988
assert first["counts"]["raw_rows"] == 18087
assert first["counts"]["rejected_rows"] == 28
assert first["counts"]["duplicate_rows"] == 31
assert first["counts"]["gaps"] == 12
# Private fixture aggregate removed when preparing the public history.
assert all(check["passed"] for check in first["checks"])
second = play(list(reversed(files)))
assert first["business_hash"] == second["business_hash"]
assert first["counts"] == second["counts"]
assert all(s["status"] == "skipped" and s["evidence"]["idempotent"] for s in second["job"]["steps"])
assert len(second["jobs"]) == 2
assert call(f"/api/lab/jobs/{first['job']['id']}")["steps"][0]["status"] == "loaded"
print(
    json.dumps(
        {
            "result": "passed",
            "loaded": 36,
            "replayed": 36,
            "business_hash": second["business_hash"],
            "counts": second["counts"],
        }
    )
)
assert call("/api/lab/reset", {"confirmation": "RESET LAB"})["status"] == "empty"
assert call("/api/lab/state")["counts"]["raw_rows"] == 0
