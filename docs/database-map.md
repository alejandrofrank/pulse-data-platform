# Pulse database map and SQL guide

The main application uses PostgreSQL database `pulse` (port 8000 API). The playback
lab uses a separate database, `pulse_lab` (port 8001 API). They use the same business
schema and ingestion code, but have independent data, IDs and publication histories.
The lab additionally has `lab.jobs` and `lab.steps`.

## From files to the screen

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"#10151c","primaryColor":"#1d3e63","primaryTextColor":"#ffffff","primaryBorderColor":"#8db9ff","secondaryColor":"#242b34","secondaryTextColor":"#ffffff","tertiaryColor":"#1d344d","tertiaryTextColor":"#ffffff","lineColor":"#8db9ff","textColor":"#ffffff","nodeTextColor":"#ffffff","edgeLabelBackground":"#10151c","rowOdd":"#242b34","rowEven":"#1d344d","attributeBackgroundColorOdd":"#242b34","attributeBackgroundColorEven":"#1d344d","relationColor":"#8db9ff","relationLabelColor":"#ffffff","relationLabelBackground":"#10151c"}}}%%
flowchart TD
    MASTER[asesorias.csv] --> ARCHIVE[raw.master: original master bytes]
    ARCHIVE --> FIRMS[core.firms: firm dimension]
    CSV[metricas_YYYY-MM.csv and numbered revisions] --> TRIGGER
    TRIGGER[Dagster sensor / schedule / manual ingestion] --> INGEST[Python ingestion: hash, parse, normalize, validate]
    INGEST --> DELIVERY[raw.deliveries: original CSV bytes and loaded_at]
    DELIVERY --> RECORDS[raw.records: one source row, original and normalized values]
    INGEST --> FILEERROR[ops.file_errors: rejected whole files and original bytes]
    RECORDS --> ISSUES[ops.issues: errors, warnings and duplicate diagnostics]
    RECORDS --> WINNER[Choose latest accepted row per firm and metric month]
    FIRMS --> WINNER
    WINNER --> FACT[mart.firm_month: current published facts]
    WINNER --> PUB[ops.publications + publication_deliveries: publication evidence]
    FACT --> COVERAGE[mart.coverage: available and missing firm/months]
    PUB --> COVERAGE
    FIRMS --> COVERAGE
    FACT --> NETWORK[mart.network_month: monthly network totals]
    COVERAGE --> NETWORK
    FACT --> API[Read-only FastAPI reporting routes]
    NETWORK --> API
    API --> HOME[Home dashboard and business agent]
    DELIVERY --> INVESTIGATE[Investigation service: source to API comparisons]
    RECORDS --> INVESTIGATE
    FACT --> INVESTIGATE
    API --> INVESTIGATE
    LAB[Lab playback: manually selected files] --> LABDB[Same ingestion and schema in isolated pulse_lab]
    LABDB --> LABUI[Lab page, monthly results and SQL editor]
```

New files are archived incrementally. Publishing rebuilds the small current fact
table in one transaction: readers see the previous or next complete publication.
Rejected rows remain queryable in raw; they cannot become current facts. If a newer
delivery is invalid, an older accepted fact can remain with
`has_rejected_newer_delivery = true`. If there is no valid candidate, coverage is
missing. Missing is not zero.

`mes` is the **business month**. `raw.deliveries.loaded_at` is **when the file was
archived**; `ops.publications.published_at` is **when it was published**. These times
answer different questions. Changing the lab cursor selects files; it does not
rewind the database. Publication membership supports historical reconstruction;
`mart.firm_month` itself holds only the current result.

## All application tables and foreign keys

This diagram covers all 13 application tables in `pulse`. Key and representative
data columns are shown; exact complete column definitions are in the linked
migrations below. `PK` = primary key, `FK` = foreign key, `UK` = unique key.
Relationships here are actual SQL foreign keys, not inferred joins inside JSON.
An unconnected table is intentional.

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"#10151c","primaryColor":"#1d3e63","primaryTextColor":"#ffffff","primaryBorderColor":"#8db9ff","secondaryColor":"#242b34","secondaryTextColor":"#ffffff","tertiaryColor":"#1d344d","tertiaryTextColor":"#ffffff","lineColor":"#8db9ff","textColor":"#ffffff","nodeTextColor":"#ffffff","edgeLabelBackground":"#10151c","rowOdd":"#242b34","rowEven":"#1d344d","attributeBackgroundColorOdd":"#242b34","attributeBackgroundColorEven":"#1d344d","relationColor":"#8db9ff","relationLabelColor":"#ffffff","relationLabelBackground":"#10151c"}}}%%
erDiagram
    ops_runs ||--o{ raw_deliveries : archives
    ops_runs ||--o{ ops_file_errors : records
    raw_deliveries ||--o{ raw_records : contains
    raw_records ||--o{ ops_issues : has
    core_firms ||--o{ mart_firm_month : has
    raw_records ||--o| mart_firm_month : selected_source
    ops_runs ||--o{ mart_firm_month : publishes
    ops_runs ||--o| ops_publications : publication
    ops_publications ||--o{ ops_publication_deliveries : includes
    raw_deliveries ||--o{ ops_publication_deliveries : included_in
    agents_conversations ||--o{ agents_runs : contains

    public_schema_migrations {
        text name PK
        text sha256
        timestamptz applied_at
    }
    raw_master {
        text sha256 PK
        bytea content
        timestamptz loaded_at
    }
    core_firms {
        integer id PK
        text nombre
        text cif
        text provincia
        text ciudad
        date fecha_alta
        integer num_empleados
        text especialidad
    }
    ops_runs {
        uuid id PK
        text dagster_run_id "External reference, not FK"
        text status
        text policy_version
        timestamptz started_at
        timestamptz finished_at
        integer files_loaded
        integer files_skipped
        integer files_failed
        text message
    }
    raw_deliveries {
        bigint id PK
        text filename UK
        date delivery_month "Unique together with revision"
        integer revision
        text sha256
        bytea content
        integer schema_version
        text policy_version
        uuid run_id FK
        timestamptz loaded_at
    }
    raw_records {
        bigint id PK
        bigint delivery_id FK "Unique together with line_number"
        integer line_number
        jsonb payload "Original CSV strings and field names"
        jsonb normalized "Canonical names and parsed values"
        text status "accepted, rejected, duplicate"
    }
    ops_issues {
        bigint id PK
        bigint record_id FK
        text severity
        text code
        text field
        text message
    }
    ops_file_errors {
        bigint id PK
        text filename
        text sha256
        bytea content
        text reason
        uuid run_id FK
        timestamptz first_seen_at
        timestamptz last_seen_at
        timestamptz resolved_at
    }
    mart_firm_month {
        integer asesoria_id PK, FK
        date mes PK "One row per firm and month"
        integer clientes_activos
        integer clientes_nuevos
        integer clientes_baja
        numeric facturacion_asesoria_eur
        numeric facturacion_gestion_eur
        numeric facturacion_consultoria_eur
        numeric facturacion_otros_eur
        numeric horas_trabajadas
        numeric satisfaccion_cliente "Nullable, otherwise 1 to 5"
        bigint source_record_id FK, UK
        boolean has_rejected_newer_delivery
        uuid published_run_id FK
    }
    ops_publications {
        uuid id PK, FK
        timestamptz published_at
        text policy_version
        bigint record_count
    }
    ops_publication_deliveries {
        uuid publication_id PK, FK
        bigint delivery_id PK, FK
    }
    agents_conversations {
        uuid id PK
        text owner
        text profile
        jsonb state
        timestamptz created_at
        timestamptz updated_at
    }
    agents_runs {
        uuid id PK
        uuid conversation_id FK
        bigint_array authorization_firm_ids
        boolean authorization_can_investigate
        text owner
        text profile
        text question
        text request_hash
        text status
        numeric reserved_cost_usd
        numeric actual_cost_usd
        jsonb result "Evidence references are JSON, not SQL FKs"
        timestamptz created_at
        timestamptz finished_at
    }
```

Diagram identifiers replace the schema separator with `_`: `raw_records` means
SQL table **`raw.records`**, for example. `mart.firm_month` also stores declaration,
consultation, incident and document counts; see its complete definition in
[001_core.sql](../backend/01_pipeline/src/pulse/migrations/001_core.sql).
The master archive produces `core.firms` through ingestion, without a foreign key.
Raw JSON has no foreign key to firms, so it can retain invalid customer IDs.
One raw row can have several issues: joining all issues before summing revenue
would duplicate amounts. Use `EXISTS` when you only need to filter affected rows.

| Relation | One row means |
| --- | --- |
| `public.schema_migrations` | One applied, checksummed migration |
| `raw.master` | One archived master file checksum |
| `core.firms` | One advisory firm; current static dimension, not SCD2 |
| `ops.runs` | One ingestion attempt |
| `raw.deliveries` | One successfully parsed delivery file/revision |
| `raw.records` | One CSV line, including rejected and duplicate lines |
| `ops.issues` | One diagnostic for one source line |
| `ops.file_errors` | One quarantined filename/checksum/reason combination |
| `mart.firm_month` | One currently published firm/month |
| `ops.publications` | One completed publication transaction |
| `ops.publication_deliveries` | One delivery available to one publication, including rejected candidates |
| `agents.conversations` | One agent conversation |
| `agents.runs` | One agent request and its cost/result audit |

There are also **three views**, calculated from the tables rather than separately
loaded: `raw.record_identity` identifies firm/month for diagnostic queries,
including some malformed rejected rows; `mart.coverage` exposes expected
firm/months and whether they have a fact; `mart.network_month` aggregates each
month with coverage and contributor counts. Its `revenue_eur` is the sum of the
four revenue components. `revenue_eur` is not a column of `mart.firm_month`.

Additional schema definitions:
[reporting views](../backend/01_pipeline/src/pulse/migrations/002_reporting.sql),
[publication evidence](../backend/01_pipeline/src/pulse/migrations/003_publication_evidence.sql),
[agent storage](../backend/01_pipeline/src/pulse/migrations/004_agent_runtime.sql),
[record identity](../backend/01_pipeline/src/pulse/migrations/005_rejected_record_identity.sql).
`public.schema_migrations` is created by
[db.py](../backend/01_pipeline/src/pulse/db.py).

## Additional tables in the isolated lab

```mermaid
%%{init: {"theme":"base","themeVariables":{"darkMode":true,"background":"#10151c","primaryColor":"#1d3e63","primaryTextColor":"#ffffff","primaryBorderColor":"#8db9ff","secondaryColor":"#242b34","secondaryTextColor":"#ffffff","tertiaryColor":"#1d344d","tertiaryTextColor":"#ffffff","lineColor":"#8db9ff","textColor":"#ffffff","nodeTextColor":"#ffffff","edgeLabelBackground":"#10151c","rowOdd":"#242b34","rowEven":"#1d344d","attributeBackgroundColorOdd":"#242b34","attributeBackgroundColorEven":"#1d344d","relationColor":"#8db9ff","relationLabelColor":"#ffffff","relationLabelBackground":"#10151c"}}}%%
erDiagram
    lab_jobs ||--o{ lab_steps : contains
    lab_jobs {
        uuid id PK
        bigint sequence UK
        jsonb filenames
        integer interval_ms
        text status
        integer cursor
        boolean pause_requested
        boolean cancel_requested
        text message
        timestamptz created_at
        timestamptz updated_at
    }
    lab_steps {
        uuid job_id PK, FK
        integer position PK
        text filename
        text status
        timestamptz started_at
        timestamptz finished_at
        jsonb evidence
        text message
    }
```

One job is an ordered playlist; one step is one file in that playlist.
`lab.steps.evidence` records before/after counts, integrity and replay results.
There are no cross-database foreign keys. Dagster separately maintains its own
framework-managed `dagster` database for orchestration state; it is not the
business data store depicted above. Test runs use disposable `pulse_test` data.

## Where and what to query

- Lab: open `http://127.0.0.1:8001/#sql-console`. Queries run against **only the lab's
  loaded files**. Run one SQL statement at a time, or select the statement first.
- Main: from the repository terminal, run
  `docker compose exec db psql -U pulse_reader -d pulse`. This reads the same
  business database as the main dashboard. There is no SQL editor endpoint on
  the main API. The reader cannot access private `agents.*` conversation tables.
- Always qualify table names with their schema, for example `mart.firm_month`.
  The lab editor's table browser lists the actual readable columns.

**Satisfaction:** the canonical field is `satisfaccion_cliente`. Valid non-missing
values are 1 through 5, inclusive. `<= 1 OR >= 1` matches every non-null numeric
value; invalid values require **`< 1 OR > 5`**. `NULL` requires `IS NULL`.

```sql
-- Published valid records with the lowest possible score.
SELECT *
FROM mart.firm_month
WHERE satisfaccion_cliente <= 1
ORDER BY mes, asesoria_id;

-- This must return no rows: these values are rejected before publication.
SELECT *
FROM mart.firm_month
WHERE satisfaccion_cliente < 1 OR satisfaccion_cliente > 5;
```

To inspect **rejected** scores such as `8,4`, query the raw archive and its recorded
validation rule. This avoids casting arbitrary CSV strings to numbers and avoids
counting a source row twice if it has multiple diagnostics:

```sql
SELECT r.id AS record_id,
       r.normalized->>'asesoria_id' AS asesoria_id,
       r.normalized->>'mes' AS mes,
       COALESCE(r.payload->>'satisfaccion_cliente',
                r.payload->>'satisfaccion_media') AS original_score,
       r.normalized->>'satisfaccion_cliente' AS normalized_score,
       r.status, d.filename, r.line_number, d.loaded_at
FROM raw.records r
JOIN raw.deliveries d ON d.id = r.delivery_id
WHERE EXISTS (
    SELECT 1 FROM ops.issues i
    WHERE i.record_id = r.id AND i.code = 'satisfaction_range'
)
ORDER BY d.loaded_at, r.line_number;
```

`payload` preserves the input name and comma decimal; `normalized` uses the
canonical name. JSON `->>` extracts text. Empty optional satisfaction is a
different case: query `mart.firm_month WHERE satisfaccion_cliente IS NULL`.
For other rejection causes inspect `ops.issues.code`, `field` and `message`.

Copyable queries for satisfaction, revenue, provenance and the live schema are in
[database-queries.sql](database-queries.sql). A record ID from the main dashboard
need not exist in the lab: identify business data using firm and month instead.
