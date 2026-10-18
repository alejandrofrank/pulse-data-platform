CREATE VIEW mart.coverage AS
WITH months AS (
    SELECT generate_series(min(delivery_month),max(delivery_month),interval '1 month')::date AS mes
    FROM raw.deliveries
)
SELECT c.id AS asesoria_id, m.mes,
       CASE WHEN f.asesoria_id IS NULL THEN 'missing' ELSE 'available' END AS status
FROM months m CROSS JOIN core.firms c
LEFT JOIN mart.firm_month f ON f.asesoria_id=c.id AND f.mes=m.mes
WHERE date_trunc('month',c.fecha_alta)::date <= m.mes;

CREATE VIEW mart.network_month AS
WITH expected AS (
    SELECT mes, count(*) AS expected_firms,
           count(*) FILTER (WHERE status='missing') AS missing_firms
    FROM mart.coverage GROUP BY mes
), aggregates AS (
    SELECT mes, count(*) AS reporting_firms,
      sum(clientes_activos) AS clientes_activos,
      sum(clientes_nuevos) AS clientes_nuevos,
      sum(clientes_baja) AS clientes_baja,
      sum(declaraciones_renta) AS declaraciones_renta,
      sum(declaraciones_iva) AS declaraciones_iva,
      sum(declaraciones_sociedades) AS declaraciones_sociedades,
      sum(declaraciones_otros) AS declaraciones_otros,
      sum(consultas_recibidas) AS consultas_recibidas,
      sum(consultas_resueltas) AS consultas_resueltas,
      sum(incidencias_aeat) AS incidencias_aeat,
      sum(documentos_procesados) AS documentos_procesados,
      count(documentos_procesados) AS documents_reporting_firms,
      sum(facturacion_asesoria_eur) AS facturacion_asesoria_eur,
      sum(facturacion_gestion_eur) AS facturacion_gestion_eur,
      sum(facturacion_consultoria_eur) AS facturacion_consultoria_eur,
      sum(facturacion_otros_eur) AS facturacion_otros_eur,
      sum(facturacion_asesoria_eur + facturacion_gestion_eur +
          facturacion_consultoria_eur + facturacion_otros_eur) AS revenue_eur,
      sum(horas_trabajadas) AS horas_trabajadas,
      count(horas_trabajadas) AS hours_reporting_firms,
      round(avg(satisfaccion_cliente),2) AS satisfaction_firm_average,
      count(satisfaccion_cliente) AS satisfaction_reporting_firms,
      count(*) FILTER (WHERE has_rejected_newer_delivery) AS fallback_firms
    FROM mart.firm_month GROUP BY mes
)
SELECT e.mes,e.expected_firms,e.missing_firms,
       coalesce(a.reporting_firms,0) AS reporting_firms,
       round(100.0 * coalesce(a.reporting_firms,0)/nullif(e.expected_firms,0),2) AS coverage_pct,
       a.clientes_activos,a.clientes_nuevos,a.clientes_baja,
       a.declaraciones_renta,a.declaraciones_iva,a.declaraciones_sociedades,a.declaraciones_otros,
       a.consultas_recibidas,a.consultas_resueltas,a.incidencias_aeat,
       a.documentos_procesados,a.documents_reporting_firms,
       a.facturacion_asesoria_eur,a.facturacion_gestion_eur,
       a.facturacion_consultoria_eur,a.facturacion_otros_eur,a.revenue_eur,
       a.horas_trabajadas,a.hours_reporting_firms,
       a.satisfaction_firm_average,a.satisfaction_reporting_firms,a.fallback_firms
FROM expected e LEFT JOIN aggregates a USING(mes);

