# Pulse Data Platform: public edition

Pulse Data Platform is an independent portfolio project demonstrating auditable
ingestion, data quality, reporting, playback and bounded analytical agents. Its public
home is [alejandrofrank/pulse-data-platform](https://github.com/alejandrofrank/pulse-data-platform).
Python package names remain `pulse`, `pulse_api` and `pulse_agents`.

## Synthetic provenance

The public dataset is wholly synthetic. Fictional firm identities and monthly metrics
are generated locally by `backend/03_scripts/generate_demo_data.py`; they are not
anonymized or renamed copies of private records. No real customer data is included.
The generator must not read private CSVs, databases, credentials or provider services.

The intended scope is 500 fictional firms and 36 months, April 2023 through March 2026.
Deliberate corrections, duplicates, rejected rows, missing optional values and gaps
exercise the [data contract](data-contract.md). These scope targets are not measured
publication counts, real business observations or performance benchmarks.

`data/manifest.json` is authoritative for the generated fixture's provenance,
inventory, checksums and expected results. Exact counts and issue distributions must
come from that manifest and fresh verification, not earlier development reports.
The manifest describes a synthetic edition; it does not add an external delivery
protocol or backfill database publication history.

From the public repository root, generate or check the fixture explicitly:

```sh
python backend/03_scripts/generate_demo_data.py --output data
python backend/03_scripts/generate_demo_data.py --output data --check
```

Generation writes the synthetic fixture. `--check` is the verification path for an
existing fixture. If the generator or manifest is absent, the public release is
incomplete; do not substitute private data. Generation and runtime checks are
**pending fresh public edition verification**, as tracked in
[verification.md](verification.md).

## Preserved development history

Before the public-edition additions, history filtering retained all 65 development
commits and 24 branches, including their parent/merge topology and author dates.
This preserves the progression of the implementation across branches.

Filtering removed the original `data/` tree and private project/evidence documents
from every retained branch and historical commit. The original source dataset is
excluded throughout the public history; the new public data is synthetic. Earlier
verification artifacts, private conversations and exact original commit/PR records
are not published as evidence for this edition.

Rewriting contents also rewrote commit hashes. Original cryptographic signatures
are not preserved as valid signatures of the rewritten commits. Pull-request metadata,
review discussions, approvals, private CI links and original commit identifiers are
not preserved by Git topology and are not reproduced here. Author dates describe
development history, not the date of public verification.

Historical branches predate the synthetic fixtures. They are code history, not
reproducible fixture snapshots, and may refer to inputs that are no longer present.
Use the current public `main` and its matching generator/manifest for the demo.
Do not reconnect an old branch to the private source dataset to make it runnable.

## Runtime isolation and verification

The public Compose project is `pulse-portfolio`; its runtime image is
`pulse-portfolio-app:local`. Run Compose from this checkout so its relative mounts
refer to public files. Its named volumes and networks belong to that project.
Keep project names distinct from any other local deployment.

Default browser ports are 8000 for the application, 3000 for Dagster and 8001 for
the optional lab. Set `PULSE_PORT`, `DAGSTER_PORT` and `LAB_PORT` to unused ports for
side-by-side operation. Port changes do not change the container's internal ports.
Use the same environment for subsequent Compose commands and explicit public URLs
for HTTP verification. See the [startup guide](../README.md#start).

No live provider is required for ingestion, dashboards, playback, exact investigations
or the documented agent demos. Optional live planning requires separately configured
credentials; no personal workstation key or existing account is assumed.

Historical tests and earlier dataset totals do not certify this edition. The
[verification guide](verification.md) separates generated expectations, measured
results and unresolved checks. Record fresh evidence against the public revision and
matching manifest before claiming a pass.

## Third-party notices

Bundled [Inter Tight](../frontend/01_dashboard/public/inter-tight-OFL.txt) and
[JetBrains Mono](../frontend/01_dashboard/public/jetbrains-mono-OFL.txt) retain their
SIL Open Font License notices. Those notices apply to the fonts. This edition
does not add a project-wide software license or assume that the project is MIT-licensed.
