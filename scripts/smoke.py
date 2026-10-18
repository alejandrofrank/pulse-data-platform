"""Exercise the real HTTP stack and compare stable published results across reruns."""

import argparse
import json
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--base", default="http://localhost:8000")
parser.add_argument("--save", type=Path)
parser.add_argument("--compare", type=Path)
args = parser.parse_args()


def get(path):
    with urllib.request.urlopen(args.base + path, timeout=15) as response:
        assert response.status == 200
        return json.load(response)


assert get("/health")["status"] == "ok"
assert get("/api/v1/firms")["total"] == 500
network = get("/api/v1/network/metrics?limit=500")
assert network["total"] == 36
quality = get("/api/v1/quality/summary")
assert quality["curated_rows"] == 17988
assert quality["latest_run"]["status"] == "success"
assert quality["file_errors"] == 0
assert get("/api/v1/quality/deliveries")["total"] == 36
for path in ("/", "/static/app.js", "/static/styles.css", "/openapi.json"):
    with urllib.request.urlopen(args.base + path, timeout=15) as response:
        assert response.status == 200
stable = {
    "network": network,
    "quality": {
        k: v for k, v in quality.items() if k not in ("latest_run", "last_successful_load")
    },
}
if args.compare:
    assert json.loads(args.compare.read_text()) == stable, "Published output changed on rerun"
if args.save:
    args.save.write_text(json.dumps(stable, sort_keys=True, indent=2))
print(
    json.dumps(
        {
            "result": "passed",
            "firms": 500,
            "months": 36,
            "curated_rows": quality["curated_rows"],
            "latest_month_revenue": network["items"][-1]["revenue_eur"],
        }
    )
)
