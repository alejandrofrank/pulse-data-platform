# Explaining Pulse in the technical defense

## A concise opening

“I separated immutable deliveries from the current reporting result. Each published
number points to a source row, a file checksum and a validation decision. Ingestion
is incremental at the file level; the reporting table is rebuilt transactionally
because 18,000 rows is small and correctness is easier to establish that way. Dagster
coordinates execution, PostgreSQL holds the audit and reporting state, and the API
and dashboard make both the business data and its limitations visible.”

## 1. Source adapters: interpreting the files

File: `src/pulse/parsing.py`.

The producer changed separator, decimal convention, a column name and revenue
categories. An explicit header-to-contract adapter prevents accidental mappings.
Both versions produce the same canonical field names. Decimal preserves money
exactly; numeric parsing rejects ambiguous thousands separators instead of guessing.

Pure parsing functions have no database side effects. This makes edge cases fast to
test and keeps rules independent of FastAPI or Dagster. The data contract documents
choices that require business confirmation: negative revenue, missing optional fields,
new other-revenue semantics and the meaning of unresolved duplicate conflicts.

## 2. Raw archive: being able to explain any number

Tables: `raw.master`, `raw.deliveries`, `raw.records`.

A SHA-256 checksum identifies file contents. The original bytes survive on disk in
Git and in the database archive; payload JSON preserves field values, while normalized
JSON holds typed canonical representations (decimal strings in JSON). Source line
numbers connect a dashboard/API result to the original CSV.

This avoids destructive cleaning. Correcting a parser later does not require asking
the producer for its old exports. A validation-policy change still requires a controlled
reprocessing/migration; it must not silently mix incompatible past decisions.

The raw representation uses JSON because the two schemas differ. The published fact
uses typed SQL columns and constraints because consumers need a stable contract.

## 3. Quality: rejection, warning and absence

Tables: `ops.issues`, `ops.file_errors`; view: `mart.coverage`.

- Rejection excludes the row from candidate publication and retains every reason.
- Warning retains the row but exposes uncertainty, such as an unknown optional value.
- Duplicate retains the evidence but contributes no additional business value.
- Coverage gaps indicate that no accepted row exists for an expected firm-month.
- File errors preserve unreadable or unexpectedly changed files as bytes.

One row can have multiple errors. Therefore “28 rejected rows” need not equal the
number of issue records. A missing score is different from an impossible score: NULL
does not assert a false answer, while 55 violates the provided 1–5 scale.

Resolved queries can exceed incoming queries because of earlier backlog. Seasonal
peaks and zero consultancy revenue are not rejected just for looking unusual.

## 4. Publication: latest valid delivery wins

File: `src/pulse/ingest.py`; table: `mart.firm_month`.

`(asesoria_id, mes)` is the business key. `(delivery_month, revision)` establishes
delivery precedence. Row order and ingestion time do not establish business precedence.

Example: March re-delivers January's record. January is replaced by March's accepted
version. Reprocessing February afterwards cannot overwrite it. Both source versions
remain visible through the lineage endpoint.

If March's correction is invalid, January's prior valid version is retained and flagged.
This is a deliberate “last valid” policy, not silent acceptance or silent fallback.
Ask the business whether withholding the number would be preferred in production.

Raw ingestion commits per delivery. Reporting rebuild commits once. If publication
fails, the old report survives and the new raw deliveries can be retried. Session-level
advisory locking protects the whole ingestion from overlapping manual/scheduled runs.

## 5. Dimensional model and aggregation

`core.firms` is the supplied firm dimension; `mart.firm_month` is a periodic snapshot
fact with some monthly flows. Month is a DATE at month start. A separate calendar
dimension is unnecessary for 36 calendar months with no fiscal-calendar attributes;
the coverage view generates the complete expected month grid.

Revenue is additive across firms/months subject to reporting completeness. Active
clients is a monthly stock; summing it across months would double-count persistent
clients. It is also not a distinct count of all people across firms. Satisfaction
is a mean of firm means because response counts do not exist. The schema does not
pretend that the current master tells us historical employee counts.

The API exposes all four revenue components and their total. Hours, documents and
satisfaction expose contributor counts. Missing months have null metrics, not zero.

## 6. Dagster: executing and observing the pipeline

Files: `src/pulse/definitions.py`, `orchestration/`, `compose.yaml`.

The ingestion/publication workflow is one Dagster asset because it has a single
transactional reporting outcome. It calls ordinary Python functions rather than
embedding business logic in orchestration decorators. A blocking asset check verifies
that the publication has rows and every winner is accepted.

Dagster records runs and structured events in its own PostgreSQL database. The daemon
evaluates the daily Europe/Madrid schedule and executes queued work. Manual CLI runs
also use the same persistent Dagster instance, so they remain visible in the UI.
No Docker socket is exposed; local subprocess execution inside the Dagster containers
is sufficient for this workload. Structured run history persists; raw stdout compute
log files inside individual containers are not a long-term log archive.

The SQL `ops.runs` ledger provides a stable application-facing status independent of
Dagster's internal schema. The dashboard can show failures without knowing GraphQL.

## 7. API and dashboard: consuming one source of truth

Files: `src/pulse/api.py`, `src/pulse/static/`.

FastAPI validates month and pagination parameters. Database queries are parameterized,
read-only, bounded and timed out. Repeatable-read transactions keep a list's total and
items consistent while a publication commits. Financial values are JSON decimal strings;
JavaScript converts them only for display, never for authoritative aggregation.

The single screen has a month range, firm selector, exact-value table, revenue chart,
quality counts, paginated source issues and a detail dialog. Select a firm and click
“Origen” on a month to inspect every source version. All source strings are rendered
with textContent, not interpreted as HTML.

The UI is intentionally a thin API client with no duplicated metric logic, frontend
dependency chain or cloud assets. Its purpose is inspectability, not visual decoration.

## 8. Packaging, CI and meaningful tests

Docker Compose waits for the database, runs migrations and initial ingestion, then
starts the serving/orchestration processes. Runtime containers use a non-root user.
The application serves the UI, avoiding an unnecessary webserver container. The
database reader role cannot write the data tables.

The CI builds test and runtime image targets, runs Ruff and pytest, starts the actual
services and makes real HTTP calls. It repeats ingestion and compares stable results.
An independent CSV calculation checks every monthly revenue/client total against the
API. That is more valuable than a test that simply calls the same aggregation twice.
Development-only jsdom tests execute the dashboard's actual JavaScript against the
live API. They check behavior and safe DOM rendering; they are not visual browser screenshots.

## Demo sequence

The optional playback lab at port 8001 makes the ingestion boundary visible. Scrubbing
selects source deliveries; it does not time-travel the published database. A separate
database starts empty, and a durable worker calls the same ingestion function once per
selected CSV. This demonstrates incremental raw archival, corrections, replay idempotency
and recovery without modifying the already-loaded dashboard. The worker controls playback
timing; Dagster remains responsible for the normal pipeline's schedule.

For a short lab demonstration, select April and May 2023, play them and inspect the
May evidence for changes to earlier firm-months. Replay both: raw counts and the
business hash stay identical. Reset the lab, load May before April, and use the lineage
API for firm 218 in April to show that delivery precedence beats arrival order. Pause
after a file, refresh the browser and continue to demonstrate persisted progress.

1. Start with `docker compose up`; open the dashboard and explain coverage.
2. Inspect a row error, a backlog warning and a duplicate.
3. Select firm 218 and inspect April 2023 lineage to show a later correction.
4. Run `docker compose run --rm bootstrap pulse ingest`; numbers stay unchanged,
   while the new execution shows the files were already processed.
5. Open Dagster and inspect `monthly_metrics`, its check and the enabled schedule.
6. Show the API docs and one missing-month response (firm 409, May 2023).
7. Show green CI and the independent reconciliation test.

## Questions to be ready for

- Why isn't NULL zero? Unknown data and known absence are different business assertions.
- Why not reject all unusual rows? Backlog and seasonality are legitimate explanations.
- Why not use file modification time? Copying files changes it; it is not delivery precedence.
- Why not incremental reporting? At this size, full deterministic publication is cheap
  and makes corrections, retries and out-of-order delivery easier to prove correct.
- Why no cloud? The deliverable is Compose/on-premise; a cloud account adds no required capability.
- Why no dbt? The transformations are few and explicit SQL, versioned and tested. Add
  dbt when the dependency graph, model documentation and team workflow justify it.
- What if facts change after a parser fix? Archive versioned source, reproduce old and
  new results, review differences, then promote a new policy and reporting version.
