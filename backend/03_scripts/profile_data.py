"""Reproducible profile of supplied files using the documented parser policy."""

import json
from collections import Counter
from pathlib import Path

from pulse.parsing import parse_drop, parse_master

root = Path("data")
firms = parse_master((root / "asesorias.csv").read_bytes())
counts = Counter()
issues = Counter()
versions = Counter()
keys = set()
for path in sorted((root / "drops").glob("*.csv")):
    version, rows = parse_drop(path.read_bytes(), path.name, firms)
    versions[version] += 1
    for row in rows:
        counts[row.status] += 1
        issues.update(f"{i.severity}:{i.code}:{i.field}" for i in row.issues)
        if row.status == "accepted":
            keys.add((row.values["asesoria_id"], row.values["mes"]))
counts["curated_keys"] = len(keys)
print(
    json.dumps(
        {"firms": len(firms), "files_by_format": versions, "rows": counts, "issues": issues},
        indent=2,
    )
)
