"""Independent, read-only CSV oracle for ingestion policy 1.

Do not import the pipeline parser here: disagreement is evidence, not a repair.
This verifies one delivery, including within-file eligibility and deduplication;
publication manifests and winner selection across deliveries belong to the caller.
"""

import csv
import io
import re
from datetime import date
from decimal import Decimal

VERIFIER_VERSION = "policy-1/oracle-2"


class VerificationLimit(ValueError):
    """Evidence exceeds the verification budget; no partial result is valid."""


_COUNT_FIELDS = (
    "clientes_activos",
    "clientes_nuevos",
    "clientes_baja",
    "declaraciones_renta",
    "declaraciones_iva",
    "declaraciones_sociedades",
    "declaraciones_otros",
    "consultas_recibidas",
    "consultas_resueltas",
    "incidencias_aeat",
    "documentos_procesados",
)
_MONEY_FIELDS = (
    "facturacion_asesoria_eur",
    "facturacion_gestion_eur",
    "facturacion_consultoria_eur",
    "facturacion_otros_eur",
)
_FIELDS = _COUNT_FIELDS + _MONEY_FIELDS + ("horas_trabajadas", "satisfaccion_cliente")
_NULLABLE = frozenset(("horas_trabajadas", "documentos_procesados", "satisfaccion_cliente"))
_HEADER_V1 = (
    "asesoria_id",
    "mes",
    "clientes_activos",
    "clientes_nuevos",
    "clientes_baja",
    "declaraciones_renta",
    "declaraciones_iva",
    "declaraciones_sociedades",
    "declaraciones_otros",
    "facturacion_asesoria_eur",
    "facturacion_gestion_eur",
    "facturacion_consultoria_eur",
    "horas_trabajadas",
    "consultas_recibidas",
    "consultas_resueltas",
    "incidencias_aeat",
    "documentos_procesados",
    "satisfaccion_cliente",
)
_HEADER_V2 = (
    "asesoria_id",
    "mes",
    "clientes_activos",
    "clientes_nuevos",
    "clientes_baja",
    "declaraciones_renta",
    "declaraciones_iva",
    "declaraciones_sociedades",
    "declaraciones_otros",
    "facturacion_asesoria_eur",
    "facturacion_gestion_eur",
    "facturacion_consultoria_eur",
    "facturacion_otros_eur",
    "horas_trabajadas",
    "consultas_recibidas",
    "consultas_resueltas",
    "incidencias_aeat",
    "documentos_procesados",
    "satisfaccion_media",
)


def _issue(record: dict, code: str, *, reject: bool = True) -> None:
    record["issue_codes"].append(code)
    if reject:
        record["status"] = "rejected"


def _number(token: str, version: int, *, count: bool) -> int | Decimal:
    separator = r"\." if version == 1 else ","
    if re.fullmatch(rf"-?\d+(?:{separator}\d+)?", token) is None:
        raise ValueError("Invalid numeric syntax")
    number = Decimal(token.replace(",", "."))
    if count:
        if not 0 <= number <= 2147483647 or number != number.to_integral_value():
            raise ValueError("Invalid count")
        return int(number)
    if not 0 <= number < Decimal("10000000000000000"):
        raise ValueError("Outside numeric(18,2) range")
    # Inspect digits instead of reproducing the production quantize operation.
    # Extra trailing zeroes are exact cents, e.g. 1.2300 is valid, 1.2301 is not.
    _, digits, exponent = number.as_tuple()
    if exponent < -2 and any(digits[exponent + 2 :]):
        raise ValueError("Nonzero sub-cent precision")
    return number


def _normalize(payload: dict, version: int, delivered: date, firms: dict[int, dict]) -> dict:
    record = {"payload": payload, "normalized": {}, "status": "accepted", "issue_codes": []}
    if None in payload or None in payload.values():
        _issue(record, "column_count")
        return record
    clean = {field: value.strip() for field, value in payload.items()}
    normalized = record["normalized"]
    try:
        if re.fullmatch(r"\d+", clean["asesoria_id"]) is None:
            raise ValueError("Invalid ID")
        normalized["asesoria_id"] = int(clean["asesoria_id"])
    except ValueError:
        _issue(record, "invalid_id")
    firm = firms.get(normalized.get("asesoria_id"))
    if firm is None:
        _issue(record, "unknown_firm")
    month = None
    try:
        if re.fullmatch(r"\d{4}-\d{2}", clean["mes"]) is None:
            raise ValueError("Invalid month syntax")
        month = date.fromisoformat(clean["mes"] + "-01")
        normalized["mes"] = month
    except ValueError:
        _issue(record, "invalid_month")
    if month is not None:
        if month > delivered:
            _issue(record, "future_month")
        if firm and month < firm["fecha_alta"].replace(day=1):
            _issue(record, "before_registration")
    for field in _FIELDS:
        source_field = (
            "satisfaccion_media" if version == 2 and field == "satisfaccion_cliente" else field
        )
        token = clean.get(source_field, "")
        if not token:
            if field == "facturacion_otros_eur" and month and month < date(2025, 11, 1):
                normalized[field] = Decimal("0.00")
            else:
                normalized[field] = None
                _issue(record, "missing_value", reject=field not in _NULLABLE)
            continue
        try:
            normalized[field] = _number(token, version, count=field in _COUNT_FIELDS)
        except ValueError:
            _issue(record, "invalid_number")
            continue
        if field == "satisfaccion_cliente" and not 1 <= normalized[field] <= 5:
            _issue(record, "satisfaction_range")
    received = normalized.get("consultas_recibidas")
    resolved = normalized.get("consultas_resueltas")
    if received is not None and resolved is not None and resolved > received:
        _issue(record, "backlog_resolved", reject=False)
    return record


def _deduplicate(records: dict[int, dict]) -> None:
    groups = {}
    for record in records.values():
        if record["status"] == "accepted":
            values = record["normalized"]
            groups.setdefault((values["asesoria_id"], values["mes"]), []).append(record)
    for candidates in groups.values():
        first, *rest = candidates
        if all(candidate["normalized"] == first["normalized"] for candidate in rest):
            for candidate in rest:
                candidate["status"] = "duplicate"
                _issue(candidate, "exact_duplicate", reject=False)
        else:
            for candidate in candidates:
                _issue(candidate, "conflicting_duplicate")


def verify_delivery(
    content: bytes, filename: str, firms: dict[int, dict], *, max_records: int = 5000
) -> dict:
    """Return policy-1 evidence keyed by each CSV record's ending physical line.

    Payload preserves CSV field strings (including whitespace), missing fields as
    None, and extra fields under the None key, matching the archived payload shape.
    Header-only files return no records; ingestion's empty-delivery rejection is
    a separate file-level rule. Unknown contracts or unreadable files raise ValueError.
    More than max_records nonempty CSV records raises VerificationLimit before
    constructing the excess payload. This includes rejected and duplicate records;
    physical blank lines do not count. The caller must separately bound input bytes.
    """
    if type(max_records) is not int or max_records < 0:
        raise ValueError("max_records must be a nonnegative integer")
    match = re.fullmatch(r"metricas_(\d{4}-\d{2})(?:__\d{3})?\.csv", filename)
    if match is None:
        raise ValueError("Unsupported delivery filename")
    delivered = date.fromisoformat(match[1] + "-01")
    try:
        text = content.decode("utf-8-sig")
        stream = io.StringIO(text, newline="")
        first_line = stream.readline()
        stream.seek(0)
        delimiter = ";" if ";" in first_line else ","
        reader = csv.reader(stream, delimiter=delimiter, strict=True)
        header = tuple(next(reader, ()))
        if header == _HEADER_V1 and delimiter == ",":
            version = 1
        elif header == _HEADER_V2 and delimiter == ";":
            version = 2
        else:
            raise ValueError("Unsupported CSV header or delimiter")
        records = {}
        for cells in reader:
            if not cells:  # CSV DictReader ignores empty physical records.
                continue
            if len(records) >= max_records:
                raise VerificationLimit(
                    f"Delivery exceeds verification limit of {max_records} records"
                )
            payload = {
                field: cells[i] if i < len(cells) else None for i, field in enumerate(header)
            }
            if len(cells) > len(header):
                payload[None] = cells[len(header) :]
            records[reader.line_num] = _normalize(payload, version, delivered, firms)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError("Unreadable UTF-8 CSV") from exc
    _deduplicate(records)
    return {"schema_version": version, "records": records}
