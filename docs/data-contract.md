# Data contract and quality policy, version 1

## Measured inputs

The supplied master has 500 firms. There are 36 deliveries, April 2023 to March 2026,
with 18,087 physical data records. The original files are committed byte-for-byte.
Run `docker compose run --rm test python 03_scripts/profile_data.py` for a repeatable profile.

The format is detected from its exact header, not guessed from the metric month:

| Contract | Files | CSV delimiter | Decimal separator | Satisfaction | Other revenue |
|---|---:|---|---|---|---|
| v1 | 31 | comma | dot | satisfaccion_cliente | absent |
| v2 | 5 | semicolon | comma | satisfaccion_media | facturacion_otros_eur |

UTF-8 with an optional BOM is supported. Quoted fields are parsed by the CSV parser.
Extra/missing row fields are quarantined. Unknown headers or encodings quarantine
the entire file with its bytes; they do not silently map to the wrong columns.

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
This is a documented interpretation of the tension between “latest wins” and
“bad data must not enter”; the reviewer can choose withholding instead.

Within a delivery, identical normalized valid rows are counted once; extra copies
have status `duplicate` and an issue pointing to the original line. Conflicting
valid rows are ALL rejected: line order is not a documented delivery order. Invalid
duplicates are rejected independently and do not erase a valid row in the same file.

Registered filenames are immutable. Different bytes under an existing name are
preserved as a file error; send a numbered revision to apply a correction. Same bytes
are skipped. Changed master data is blocked pending an explicit master migration;
the assignment describes this master as clean/static, not as a historical dimension.

## Validation choices

| Condition | Decision and rationale |
|---|---|
| Unknown firm ID, malformed/nonexistent month, future metric month, month before registration | Reject; cannot establish a valid firm-month |
| Negative or fractional counts, NaN/Infinity, invalid decimal syntax, overflow | Reject; invalid for the declared metric |
| Negative monthly revenue | Reject under the MVP contract of nonnegative monthly reported income. Credits can be legitimate in other systems: confirm with Finance before changing this rule. Never silently take absolute values. |
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

## Historical fixture evidence

Private fixture measurements were removed from the public history.

## Aggregation semantics

Revenue uses Decimal / PostgreSQL NUMERIC, never binary floats for computation.
All four revenue categories are included. Monthly stock counts are summed across
firms, not across months. Active clients are firm portfolios, NOT globally deduplicated
people: the source contains no client identities. Satisfaction is an unweighted mean
of firm means, explicitly labeled; respondent weights are unavailable. SQL SUM/AVG
ignore NULL, so contribution counts and missing-firm coverage accompany aggregates.
Comparisons span an export contract change in Nov 2025; other revenue may increase
the apparent total without representing organic growth.
