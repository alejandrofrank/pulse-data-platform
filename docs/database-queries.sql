-- Pulse Data Platform: queries over the synthetic public dataset.
-- Example IDs/months are query parameters, not claims about measured fixture results.
-- Run ONE statement at a time in the lab SQL editor (or select it first).
-- Lab: http://127.0.0.1:8001/#sql-console (only files loaded in the lab).
-- Main: docker compose exec db psql -U pulse_reader -d pulse
-- Run Compose from the public checkout; use LAB_PORT if its default is overridden.

-- 1. Published records at the lowest valid satisfaction score.
SELECT *
FROM mart.firm_month
WHERE satisfaccion_cliente <= 1
ORDER BY mes, asesoria_id;

-- 2. Invalid published scores: expect no rows because validation blocks them.
SELECT *
FROM mart.firm_month
WHERE satisfaccion_cliente < 1 OR satisfaccion_cliente > 5;

-- 3. Rejected satisfaction scores, original strings and source lineage.
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
    WHERE i.record_id = r.id AND i.field = 'satisfaccion_cliente'
      AND i.severity = 'error'
)
ORDER BY d.loaded_at, r.line_number;

-- 4. Missing optional satisfaction; missing is not zero or an invalid score.
SELECT asesoria_id, mes, satisfaccion_cliente
FROM mart.firm_month
WHERE satisfaccion_cliente IS NULL
ORDER BY mes, asesoria_id;

-- 5. Example: January 2024 revenue for fictional firm 42 and its synthetic source row.
-- Choose a loaded firm/month; no rows can mean that the target has not been published.
SELECT f.asesoria_id, c.nombre, f.mes,
       f.facturacion_asesoria_eur + f.facturacion_gestion_eur
       + f.facturacion_consultoria_eur + f.facturacion_otros_eur AS revenue_eur,
       f.satisfaccion_cliente, d.filename, r.line_number, d.loaded_at,
       f.has_rejected_newer_delivery
FROM mart.firm_month f
JOIN core.firms c ON c.id = f.asesoria_id
JOIN raw.records r ON r.id = f.source_record_id
JOIN raw.deliveries d ON d.id = r.delivery_id
WHERE f.asesoria_id = 42 AND f.mes = DATE '2024-01-01';

-- 6. The network's monthly topline, with explicit coverage.
SELECT mes, revenue_eur, clientes_activos, reporting_firms,
       expected_firms, missing_firms, coverage_pct
FROM mart.network_month
ORDER BY mes;

-- 7. Real readable columns and types (agent storage needs a separate role).
SELECT table_schema, table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema IN ('raw', 'core', 'mart', 'ops', 'lab', 'agents', 'public')
ORDER BY table_schema, table_name, ordinal_position;
