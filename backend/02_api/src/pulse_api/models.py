"""Public metric response contracts, also shown in OpenAPI."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class MetricValues(BaseModel):
    mes: date
    clientes_activos: int | None
    clientes_nuevos: int | None
    clientes_baja: int | None
    declaraciones_renta: int | None
    declaraciones_iva: int | None
    declaraciones_sociedades: int | None
    declaraciones_otros: int | None
    facturacion_asesoria_eur: Decimal | None
    facturacion_gestion_eur: Decimal | None
    facturacion_consultoria_eur: Decimal | None
    facturacion_otros_eur: Decimal | None
    revenue_eur: Decimal | None
    horas_trabajadas: Decimal | None
    consultas_recibidas: int | None
    consultas_resueltas: int | None
    incidencias_aeat: int | None
    documentos_procesados: int | None


class FirmMetric(MetricValues):
    asesoria_id: int
    coverage_status: Literal["available", "missing"]
    satisfaccion_cliente: Decimal | None
    has_rejected_newer_delivery: bool | None
    source_record_id: int | None
    filename: str | None
    line_number: int | None


class NetworkMetric(MetricValues):
    expected_firms: int
    missing_firms: int
    reporting_firms: int
    coverage_pct: Decimal | None
    hours_reporting_firms: int | None
    documents_reporting_firms: int | None
    satisfaction_reporting_firms: int | None
    satisfaction_firm_average: Decimal | None
    fallback_firms: int | None


class FirmMetricPage(BaseModel):
    items: list[FirmMetric]
    total: int
    limit: int
    offset: int


class NetworkMetricPage(BaseModel):
    items: list[NetworkMetric]
    total: int
    limit: int
    offset: int
