# Pulse Data Platform: public-edition verification

**Current status: pending fresh public edition verification.**

The synthetic generator's manifest declares expectations. It does not establish that
ingestion, publication, APIs, playback or agents have passed. Earlier runs and private
evidence are not applicable to this edition. No runtime pass or benchmark is claimed
by this document.

## Verification record

Complete this record after running the checks against the public checkout and its
matching fixture. Keep failures and skipped checks visible.

| Check | Current status | Evidence to record |
|---|---|---|
| Fixture generation and `--check` | Pending | Generator version, seed, manifest SHA-256 and check output |
| Compose isolation and configuration | Pending | Project/image names, public mount paths and bound ports |
| Ruff and backend/PostgreSQL tests | Pending | Commands, exit codes, logs and test report |
| Profile and API reconciliation | Pending | Manifest expectations beside measured counts and monthly totals |
| Repeated ingestion | Pending | Before/after business results, raw inventory and new run ID |
| Agent demos and exact investigation | Pending | Demo status, executed targets, returned evidence and outcomes |
| Frontend tests and browser inspection | Pending | Test report, viewport sizes, errors and screenshots if captured |
| Lab forward/reverse replay | Pending | Disposable lab identity, raw counts and business hashes |
| Sensor and quarantined-input startup | Pending | Disposable public CI run, logs and outcomes |

Also record the UTC verification time, public Git revision, working-tree state,
runtime versions, resolved ports and artifact locations. A result applies only to
that revision and fixture. Do not mark a check passed from its existence in a workflow.

## 1. Prepare the public fixture and isolate the stack

Run commands from the `pulse-data-platform` checkout. Never point this run at another
checkout's data, volumes or existing services. Requirements are Python 3.12, Docker
Compose v2 with Linux containers, and Node 24+ for frontend reports. Initial image
and dependency installation needs network access; no provider credential is needed.

```sh
python backend/03_scripts/generate_demo_data.py --output data
python backend/03_scripts/generate_demo_data.py --output data --check
docker compose config --quiet
```

Generation writes the public synthetic fixture; run it before ingesting and retain
the generated `data/manifest.json`. If either generator or manifest is absent, leave
verification pending. Do not replace missing fixtures with private exports.

Inspect the resolved Compose configuration before starting services: the project
must be `pulse-portfolio`, the runtime image `pulse-portfolio-app:local`, and data
mounts must resolve to this public checkout. A different project name isolates named
volumes and networks; host ports still need to be unused.

Defaults are application 8000, Dagster 3000 and optional lab 8001. For a side-by-side
run, set unused ports in the same shell before any startup or test command. Example:

```powershell
$env:PULSE_PORT = "8100"
$env:DAGSTER_PORT = "3100"
$env:LAB_PORT = "8101"
$env:PULSE_TEST_URL = "http://127.0.0.1:8100"
```

`PULSE_TEST_URL` targets frontend HTTP tests; it is separate from the Compose port
variables. Commands below show default HTTP ports. Replace every `--base` and
`--dagster` URL with the public stack's chosen ports, and use its port for browser
inspection. Do not let a default URL silently test another running application.

## 2. Check source, parser and database behavior

```sh
docker compose build test
docker compose up -d --wait db
docker compose run --rm test ruff check 01_pipeline 02_api 04_agents tests 03_scripts
docker compose run --rm test ruff format --check 01_pipeline 02_api 04_agents tests 03_scripts
docker compose run --rm test pytest -q -p no:cacheprovider
docker compose run --rm test python 03_scripts/profile_data.py
```

The test image copies the fixture at build time; rebuild it after fixture changes.
Integration tests use `pulse_test` and require a database name ending in `_test`.
Run database test processes serially. Capture output and failures rather than changing
the expected result merely to match an observed failure.

Compare the profile to these manifest sections:

- `counts`: firm/month scope, base/correction rows and file inventory. `csv_files`
  includes the master; `monthly_files` counts deliveries only.
- `expected_quality`: accepted, rejected, duplicate, raw and published rows, gaps,
  fallbacks, optional nulls and diagnostic counts. Issues and affected rows are
  distinct measures; do not substitute severity counts for row counts.
- `monthly_totals`: entries keyed by `YYYY-MM`, containing `revenue_eur`,
  `clientes_activos` and `reporting_firms`.
- `scenarios`: concrete targets for corrections, gaps, warnings, rejections and
  investigations. Re-read them when the generator version or seed changes.

Record actual results separately from expected values. The manifest alone does not
prove parser correctness; the independent reconciliation checks must also agree.

## 3. Check the running application and replay

```sh
docker compose up -d --build --wait
python backend/03_scripts/smoke.py --base http://127.0.0.1:8000 --save test-results/public-before.json
docker compose run --rm bootstrap pulse ingest
python backend/03_scripts/smoke.py --base http://127.0.0.1:8000 --compare test-results/public-before.json
python backend/03_scripts/agent_smoke.py --base http://127.0.0.1:8000
```

Create the local `test-results` directory before using `--save`. Run without provider
secrets; the agent smoke requires explicit demo mode and must not spend a model key.
Inspect the quality summary, delivery inventory, gaps and all monthly network totals
against the manifest. Compare money as exact decimal values, not rounded floats.
Repeated ingestion should preserve business results and source inventory while adding
an execution audit entry; operational IDs/timestamps are not idempotence failures.

Check the actual HTTP responses, source lineage and independent investigation for a
manifest-selected firm/month. The current scenario catalogue identifies fictional
firm 42 in January 2024 for investigation. A `consistent` outcome must come from a
fresh response; it is not asserted here.

## 4. Check the interfaces

```sh
npm --prefix frontend ci
npm --prefix frontend run lint
npm --prefix frontend test
```

Set `PULSE_TEST_URL` first when the public application is not on port 8000. Inspect
the dashboard, agents and optional lab at desktop and mobile widths. Check loading,
empty and failure states, date/firm filters, literal source rendering, lineage,
conversation recovery, horizontal overflow and browser errors. DOM test results
alone are not visual verification.

For a consolidated local report, `python backend/03_scripts/test_dashboard.py` builds
and runs backend/frontend suites and writes under `test-results/dashboard/`. It
inherits `PULSE_TEST_URL`; its printed port-8000 link may need replacement with your
public application's port. Open `/tests/` there. Reports contain timestamps and
revision information; a missing, stale or interrupted report is not a pass. Do not
run this runner concurrently with another database test process.

## 5. Verify disposable playback and automatic pickup

Only use disposable public state for these checks. The lab smoke requires an
initially empty lab and clears its playback state. The sensor smoke writes a temporary
synthetic revision and leaves its archived database evidence until stack teardown.
Run the clean-fixture checks above before the sensor check changes the inventory.

```sh
docker compose --profile lab up -d --build --wait lab
python backend/03_scripts/lab_smoke.py --base http://127.0.0.1:8001 --disposable
python backend/03_scripts/smoke.py --base http://127.0.0.1:8000 --compare test-results/public-before.json
python backend/03_scripts/sensor_smoke.py --base http://127.0.0.1:8000 --dagster http://127.0.0.1:3000 --data-dir data --disposable
```

Use the public fixture manifest to interpret forward playback, reverse replay and
the expected winning corrections. The lab must not change the main publication.
Record actual skips, hashes, counts and Dagster trigger evidence.

The quarantined-input startup check, `backend/03_scripts/bootstrap_smoke.py`, is
guarded for disposable GitHub Actions execution. Leave it to the
[public CI workflow](../.github/workflows/ci.yml); do not bypass that guard locally.
The workflow definition describes intended checks, not an already successful run.

After recording evidence, stop the disposable public project from this checkout.
`docker compose --profile lab down` keeps its named volumes.
`docker compose --profile lab down -v` deletes those volumes and their run/playback
history; use it only for the deliberately disposable public stack.

## Completing this record

Replace each pending status only with a fresh observed result and its evidence.
Record failed or unavailable checks explicitly. Keep fixture expectations in the
manifest and measured results here; avoid copying static warning distributions into
other guides. Optional live model evaluation is separate, requires explicit opt-in,
and cannot substitute for the provider-free pipeline and interface checks above.
