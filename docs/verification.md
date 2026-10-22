# Pulse Data Platform: public-edition verification

Verification date: **22 October 2026**. Runtime revision:
[`6e162df`](https://github.com/alejandrofrank/pulse-data-platform/commit/6e162dfebf433a0d3044011670e86fa3e70eeda8).
The [clean-install acceptance run](https://github.com/alejandrofrank/pulse-data-platform/actions/runs/39821630853)
**passed every stage**, testing the public checkout with fresh PostgreSQL volumes
and no provider key. All 760 tests passed, with no failures, errors or skips.
The release-evidence commit changes documentation only.

## Verification record

Results below apply to the synthetic fixture and runtime revision above. The
manifest declares expectations; independent CSV, database and HTTP checks verify them.

| Check | Current status | Evidence to record |
|---|---|---|
| Fixture generation and `--check` | Passed locally and in CI | Generator v1, seed 731942; all 38 files match byte-for-byte |
| Compose isolation and configuration | Passed | `pulse-portfolio` project, independent volumes and images; fresh CI bootstrap |
| Ruff and backend/PostgreSQL tests | Passed in CI | 671 cases; real PostgreSQL, migrations and independent CSV reconciliation |
| Profile and API reconciliation | Passed | All 36 monthly totals and source coverage agree with independent expectations |
| Repeated ingestion | Passed | All 36 files skipped; source inventory and business results unchanged |
| Agent demos and exact investigation | Passed | Both agents return database evidence; firm 42 / January 2024 reconciles; retries replay saved results |
| Frontend tests and formatting | Passed | 89 cases; dashboard checks target the running synthetic API |
| Fresh desktop/mobile visual inspection | Not repeated | No new visual acceptance claim; the interfaces retain the existing layouts with synthetic-data labels |
| Lab forward/reverse replay | Passed locally and in CI | 36 loaded, 36 skipped in reverse; identical counts and business hash |
| Sensor and quarantined-input startup | Passed in CI | Real daemon pickup, unchanged-file deduplication, diagnostics available after restart with a quarantined file |

The measured dataset contains 500 firms, 36 deliveries, 18,087 raw metric rows,
18,028 accepted versions, 31 duplicates, 28 rejections, 42 rows with warnings,
17,988 published firm-months and 12 gaps. The 40 valid corrections supersede earlier
accepted versions. Fourteen keys retain an older valid version after a rejected
newer attempt. March 2026 synthetic network revenue is EUR 30,499,873.18.

Manifest SHA-256: `f60f4fe1c9ec118876da6e949e8a5ae740e766be24d19c01dc99cb8bd06e6b67`.
Local forward/reverse business hash:
`2f071ec8b9545486749ab51d7d0d50ea58027cd913f7b6aa3b4e8b4b181d267c`.
The side-by-side local run used application 8012, Dagster 3012 and lab 8013;
CI uses the default ports on its disposable runner. Python 3.12 and PostgreSQL
17.6 run in the pinned images; Node 24 runs frontend checks.

CI publishes `test-dashboard` JUnit/JSON reports and `service-logs` artifacts.
The accepted report records a clean working tree at the runtime revision above.
The local test reports are ignored by Git. An initial new CLI test attempted to
write under the image's read-only application directory; it now uses a system
temporary directory. A subsequent local playback check failed intermittently;
the test now verifies the requested job ID and reports the actual job failure
before checking its totals. The full CI suite is the acceptance record after those
changes. Two dependency deprecation warnings are recorded separately from failures.

No live model calls, concurrent-user load test or new visual certification were
performed for this public release. Later commits must be checked through their own
Actions results; this record does not claim to verify future changes.

## Public history audit

All 70 reachable commits and 350 unique blobs at the tested revision were reviewed.
The 65 original development commits retain their mapped parent relationships,
author/committer metadata and branch progression. The audit found no original CSV
rows, original firm names or tax IDs, removed private document blobs, targeted
private financial results, or private audit references in the retained history.
Credential-shaped test fixtures are explicitly fake, and database passwords are
documented local demo defaults. Twenty-three old signatures were removed by rewriting;
one original commit became empty and was retained to preserve chronology.

The subsequent release-evidence changes affect only the README and documentation;
their content and local links were checked separately. They do not change the tested
runtime, fixtures or workflow. The documentation-only merge skips another identical
runtime CI run and links the exact accepted revision above.

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
