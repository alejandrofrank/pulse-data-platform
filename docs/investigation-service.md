# Deterministic revenue investigation

`GET /api/v1/investigations/{firm_id}/{YYYY-MM}` compares one firm's total revenue
across the archived CSV, normalized record, publication and actual local HTTP API.
It does not call an LLM, modify data, or accept arbitrary SQL or URLs.

## Publication identity

Migration 003 adds `ops.publications` and `ops.publication_deliveries`. Ingestion
records the complete input-delivery manifest in the **same transaction** as the
mart replacement. Coverage now advances with publication, and includes older
corrected metric months. An archived correction whose publication failed is
explicitly reported as unpublished; it cannot be mistaken for the current winner.

Existing historical runs are not retroactively labeled as known publications.
After upgrading, run ingestion once to establish the first manifest. This skips
unchanged CSV bytes and republishes the same business data with a recorded manifest.

Metric responses include `X-Pulse-Publication-ID`. The investigator reads all its
database evidence in one repeatable-read, read-only transaction and closes it
before making the HTTP request. It compares only matching publication identifiers,
retries once on change, and otherwise reports an inconclusive comparison.

The HTTP check exercises the colocated reporting application, not an external
reverse proxy or a user's browser. `PULSE_INTERNAL_API_PORT` permits only 8000 or
8001; redirects and proxy environment variables are disabled.

## Evidence and independent verification

`pulse_api.reconciliation` implements policy 1 independently of the ingestion
parser. It reads original archived bytes, checks full-row eligibility, Decimal
values, optional fields, both export formats and full-row duplicate conflicts.
This intentional independent implementation is versioned and must evolve with
reviewed contract changes. Unsupported policy versions are not guessed.

The service compares archive checksum, stored raw payload, normalization and
eligibility, publication winner, all four revenue components, API identity and
serialized totals. Original CSV locations are **ending physical line numbers**:
quoted records can span several lines. All executed evidence SQL and parameters
are returned. The evidence digest identifies this collected packet; operational
IDs and observation times mean it is not a business fingerprint across rebuilds.

Expected delivery precedence is parsed independently from filenames and compared
with stored delivery month/revision metadata. The full accepted candidate set must
fit the verification budget before a winner can be certified. An omitted payload
or normalized field remains unavailable evidence, never an empty value to compare
as if it were complete. API item counts and missing-month markers are checked
against the same database snapshot's coverage, including when no accepted row exists.

Each check is `match`, `mismatch` or `unavailable`. The overall outcome is
`discrepancy` if any check disagrees, `inconclusive` if evidence is unavailable,
otherwise `consistent`. A consistent result only means that the checked layers
agree with the recorded contract. Supplier financial correctness needs independent
business evidence. A mismatch is an investigation finding, never an automatic repair.

## Limits and explicit uncertainty

- At most two investigations per API process, a five-second statement timeout,
  one-second lock timeout, and a collection deadline checked between operations.
- At most 50 matching versions, 2 MiB per verified archive and 8 MiB of archives
  per attempt. Selected and expected records are included explicitly even when
  the history page is truncated. Each archive is limited to 5,000 parsed records;
  only relevant records are retained. Oversized stored JSON fields are omitted.
  Verification budget exhaustion is visible.
- Whole-file errors cannot safely be assigned to a firm: unresolved errors are
  shown separately with count/truncation and prevent a complete-coverage claim.
- Missing values and missing source records are never treated as zero.
- No reconstruction of historical HTTP responses, screen rendering, or arbitrary
  past policy execution is claimed. New manifests preserve input membership from
  this migration onward; they do not backfill missing historical evidence.
- Registered checksums detect disagreement, not a privileged attacker changing
  both archived bytes and their registered hash. Existing raw immutability remains
  an application convention; backups and storage controls remain operational work.

The read-only role is required outside guarded disposable `*_test` databases.
The existing local API is bound to loopback; this endpoint is an internal operator
tool, not a multi-tenant authorization boundary.

## Verification

Tests inject independent failures into archived bytes, normalized money and
eligibility, published revenue, source selection and actual API serialization.
They also cover rejected newer corrections, publication failures, absent records,
publication races, complete-file duplicate rules and explicit history truncation.
The original Phase 1 reconciliation and idempotency checks remain required.
