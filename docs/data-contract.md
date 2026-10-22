# Pulse Data Platform: data contract and quality policy, version 1

## Synthetic input contracts

The public dataset is generated from fictional firms and metrics. Its intended
scope is 500 firms and 36 monthly deliveries, April 2023 through March 2026, with
deliberate quality scenarios. These are generation targets, not measured outcomes.
`data/manifest.json` is authoritative for the generated inventory, checksums and
expected results. See [provenance](public-edition.md) and the
[verification procedure](verification.md); fresh public-edition verification is pending.

The manifest separates `counts` (inventory and generation scope) from
`expected_quality` (expected parser/publication outcomes). `counts.csv_files`
includes the master; `counts.monthly_files` counts only delivery files.
`monthly_totals` is keyed by `YYYY-MM` and stores `revenue_eur`, `clientes_activos`
and `reporting_firms`. `scenarios` supplies concrete test targets. Fictional master
identifiers marked `DEMO-*` must not be treated as real registration identifiers;
the relational `asesoria_id` remains numeric.

The format is detected from its exact header, not guessed from the metric month:

| Contract | CSV delimiter | Decimal separator | Satisfaction | Other revenue |
|---|---|---|---|---|
| v1 | comma | dot | satisfaccion_cliente | absent |
| v2 | semicolon | comma | satisfaccion_media | facturacion_otros_eur |

UTF-8 with an optional BOM is supported. Quoted fields are parsed by the CSV parser.
Extra/missing row fields are quarantined. Unknown headers or encodings quarantine
the entire file with its bytes; they do not silently map to the wrong columns.

Column-count rejections retain policy 1's empty normalized JSON. Migration 005 adds
`raw.record_identity`, a separate projection that recovers a known firm and valid
metric month from the archived payload when safely attributable. It does not accept
or infer any metrics, rewrite source history, or change the validation policy.
Quality issues and lineage expose `identified_firm_id` / `identified_month` separately
from the original payload/normalization. Recovery does not assign unknown firm IDs
or invalid months. Previously normalized keys retain their recorded values; a known
firm can remain identified even when its month is unusable.
After upgrading, the normal ingestion command refreshes prior fallback flags even
when every delivery is skipped; no raw reprocessing is required.

## Grain and precedence

One published row per `(asesoria_id, mes)`. `mes` is the metric month, independent
of delivery month. The master determines identity. No fuzzy ID matching is attempted.

Delivery names encode precedence: `metricas_YYYY-MM.csv`, optionally
`metricas_YYYY-MM__NNN.csv` for an explicitly ordered revision. Rank by delivery
month, then numeric revision. File mtime, directory enumeration and ingestion time
have no business meaning. A new delivery may correct any older month.

After validation, the latest valid delivery wins. All rejected attempts remain
queryable. If a later delivery for a known key is rejected, an older accepted value
is retained with `has_rejected_newer_delivery=true`, visible in metrics and lineage.
This is an explicit latest-valid policy. Withholding an older value after an invalid
correction would be a different contract and requires a versioned policy change.

Within a delivery, identical normalized valid rows are counted once; extra copies
have status `duplicate` and an issue pointing to the original line. Conflicting
valid rows are ALL rejected: line order is not a documented delivery order. Invalid
duplicates are rejected independently and do not erase a valid row in the same file.

Registered filenames are immutable. Different bytes under an existing name are
preserved as a file error; send a numbered revision to apply a correction. Same bytes
are skipped. Changed master data is blocked pending an explicit master migration;
this contract treats the master as a static dimension, with no historical attributes.

## Validation choices

| Condition | Decision and rationale |
|---|---|
| Unknown firm ID, malformed/nonexistent month, future metric month, month before registration | Reject; cannot establish a valid firm-month |
| Negative or fractional counts, NaN/Infinity, invalid decimal syntax, overflow | Reject; invalid for the declared metric |
| Negative monthly revenue | Reject under the demo contract of nonnegative monthly reported income. Credits can be legitimate in other systems; define their meaning before changing this rule. Never silently take absolute values. |
| Satisfaction outside 1–5 | Reject the row; retain the original value and error |
| Missing hours/documents/satisfaction | Accept with a warning and SQL NULL. Do not discard valid revenue, impute zero or invent survey answers. Aggregates expose the number of contributors. |
| Missing counts or reported revenue components | Reject; headline totals would otherwise silently understate the firm |
| Other revenue absent/blank for a metric month before Nov 2025 | Zero under the documented assumption that this category was not reported before its introduction; includes old-month corrections in the new format |
| Other revenue missing for Nov 2025 onward | Reject; a missing newly required component is not zero |
| Resolved consultations exceed received | Accept with warning: earlier backlog can be resolved this month |
| Zero consultancy income, seasonal peaks, large firms | Accept; not inherently invalid. No arbitrary statistical outlier removal. |
| Missing firm-month | Expose as a gap, not zero or interpolated data |

Client stock reconciliation across months is not enforced: transfers and unspecified
stock timing could explain differences. A production contract must define it first.
Coverage assumes all firms registered by that month are expected to report; the
master has no closure date. Extend eligibility if the source introduces closures.

## Fixture expectations and measured results

The generator deliberately exercises corrections, normalized duplicates, invalid
records, optional missing fields, backlog warnings and coverage gaps. The manifest
records expectations for that generated edition. Exact counts and any warning or
rejection breakdown must be read from the manifest and checked against a fresh
profile and ingestion; earlier fixture totals are not public-edition evidence.

Keep these measures distinct:

- Raw rows include accepted versions, rejected rows and duplicate copies.
- Accepted versions can include superseded corrections, so they need not equal
  unique published firm-months.
- Issue counts need not equal affected-row counts; one row can have several issues.
- Coverage gaps count eligible firm-months without a published fact.
- Manifest expectations describe the fixture; measured database/API results verify
  the implementation. One must not substitute for the other.

Record observed counts and their reconciliation in [verification.md](verification.md)
only after the checks run. Ingestion does not hard-code the demo's firm count or
date range. The fixture manifest is not a production delivery-completeness protocol.

## Aggregation semantics

Revenue uses Decimal / PostgreSQL NUMERIC, never binary floats for computation.
All four revenue categories are included. Monthly stock counts are summed across
firms, not across months. Active clients are firm portfolios, NOT globally deduplicated
people: the source contains no client identities. Satisfaction is an unweighted mean
of firm means, explicitly labeled; respondent weights are unavailable. SQL SUM/AVG
ignore NULL, so contribution counts and missing-firm coverage accompany aggregates.
Comparisons span an export contract change in Nov 2025; other revenue may increase
the apparent total without representing organic growth.
