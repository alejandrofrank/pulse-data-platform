-- Membership is recorded in the same transaction as publication. Old runs are
-- intentionally not backfilled: we cannot assert historical snapshots retroactively.
CREATE TABLE ops.publications (
    id uuid PRIMARY KEY REFERENCES ops.runs,
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    policy_version text NOT NULL,
    record_count bigint NOT NULL
);
CREATE INDEX publications_latest ON ops.publications(published_at DESC);
CREATE TABLE ops.publication_deliveries (
    publication_id uuid NOT NULL REFERENCES ops.publications,
    delivery_id bigint NOT NULL REFERENCES raw.deliveries,
    PRIMARY KEY (publication_id, delivery_id)
);

-- Coverage must advance with publication, not when an uncommitted-to-reporting
-- delivery happens to have reached raw. Include older corrected metric months.
CREATE OR REPLACE VIEW mart.coverage AS
WITH current_publication AS (
    SELECT id FROM ops.publications ORDER BY published_at DESC LIMIT 1
), dates AS (
    SELECT d.delivery_month AS mes FROM raw.deliveries d
    JOIN ops.publication_deliveries pd ON pd.delivery_id=d.id
    JOIN current_publication p ON p.id=pd.publication_id
    UNION SELECT mes FROM mart.firm_month
), months AS (
    SELECT generate_series(min(mes),max(mes),interval '1 month')::date AS mes FROM dates
)
SELECT c.id AS asesoria_id,m.mes,
       CASE WHEN f.asesoria_id IS NULL THEN 'missing' ELSE 'available' END AS status
FROM months m CROSS JOIN core.firms c
LEFT JOIN mart.firm_month f ON f.asesoria_id=c.id AND f.mes=m.mes
WHERE date_trunc('month',c.fecha_alta)::date <= m.mes;
