-- Policy 1 deliberately stores normalized={} for column-count rejections. Recover
-- their identity from archived payload without rewriting raw JSON, changing policy,
-- accepting metrics, or making the independent verifier reinterpret old archives.
-- Both identity columns are TEXT, matching normalized->> keys used by reporting.
CREATE VIEW raw.record_identity AS
WITH malformed AS (
    SELECT r.id AS record_id, d.delivery_month,
           regexp_replace(r.payload->>'asesoria_id',
                          '^[[:space:]]+|[[:space:]]+$', '', 'g') AS firm_token,
           CASE WHEN jsonb_typeof(r.payload->'mes')='string'
                THEN regexp_replace(r.payload->>'mes',
                                    '^[[:space:]]+|[[:space:]]+$', '', 'g') END AS month_token
    FROM raw.records r JOIN raw.deliveries d ON d.id=r.delivery_id
    WHERE r.status='rejected' AND r.normalized='{}'::jsonb
      AND jsonb_typeof(r.payload->'asesoria_id')='string'
      AND EXISTS (SELECT 1 FROM ops.issues i
                  WHERE i.record_id=r.id AND i.code='column_count' AND i.severity='error')
), candidates AS (
    SELECT m.record_id, m.delivery_month, c.id, c.fecha_alta,
           -- Guard the cast inside CASE: malformed/future dates must not break reads.
           CASE WHEN m.month_token ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'
                     AND left(m.month_token,4)<>'0000'
                THEN (m.month_token || '-01')::date END AS metric_month
    FROM malformed m JOIN core.firms c
      -- Never cast an untrusted ID to integer; unknown/overflow IDs cannot form a key.
      -- Recovery accepts ASCII digits with optional leading zeroes and whitespace.
      ON m.firm_token ~ '^[0-9]+$' AND ltrim(m.firm_token,'0')=c.id::text
), recovered AS (
    SELECT record_id, id::text AS asesoria_id,
           CASE WHEN metric_month<=delivery_month
                     AND metric_month>=date_trunc('month',fecha_alta)::date
                THEN metric_month::text END AS mes
    FROM candidates
)
SELECT r.id AS record_id,
       coalesce(r.normalized->>'asesoria_id',k.asesoria_id) AS asesoria_id,
       coalesce(r.normalized->>'mes',k.mes) AS mes
FROM raw.records r LEFT JOIN recovered k ON k.record_id=r.id;

-- A known firm with an invalid/missing month remains searchable in firm issues,
-- but has no complete key for lineage/fallback. Unknown firms recover neither key.
-- Existing normalized keys retain their recorded meaning. Archived malformed rows
-- become discoverable immediately; normal ingestion republishes their fallback flags
-- even when every file is checksum-skipped. No raw reprocessing/backfill is needed.
