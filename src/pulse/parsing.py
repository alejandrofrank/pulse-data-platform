"""Pure, deterministic CSV contracts and quality rules; no database dependencies."""

import csv
import io
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

COUNTS = (
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
REVENUE = (
    "facturacion_asesoria_eur",
    "facturacion_gestion_eur",
    "facturacion_consultoria_eur",
    "facturacion_otros_eur",
)
METRICS = COUNTS + REVENUE + ("horas_trabajadas", "satisfaccion_cliente")
OPTIONAL = {"horas_trabajadas", "documentos_procesados", "satisfaccion_cliente"}
V1 = (
    "asesoria_id",
    "mes",
    *COUNTS[:7],
    *REVENUE[:3],
    "horas_trabajadas",
    *COUNTS[7:],
    "satisfaccion_cliente",
)
V2 = (
    "asesoria_id",
    "mes",
    *COUNTS[:7],
    *REVENUE,
    "horas_trabajadas",
    *COUNTS[7:],
    "satisfaccion_media",
)
MASTER = (
    "id",
    "nombre",
    "cif",
    "provincia",
    "ciudad",
    "fecha_alta",
    "num_empleados",
    "especialidad",
)
CHANGEOVER = date(2025, 11, 1)
POLICY_VERSION = "1"


class FileContractError(ValueError):
    """A complete file is unreadable or does not match a supported contract."""


@dataclass
class Issue:
    code: str
    severity: str
    field: str | None
    message: str


@dataclass
class ParsedRow:
    line: int
    raw: dict
    values: dict = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    status: str = "accepted"

    def issue(self, code, message, column=None, severity="error"):
        self.issues.append(Issue(code, severity, column, message))
        if severity == "error":
            self.status = "rejected"


def delivery_key(name: str) -> tuple[date, int]:
    match = re.fullmatch(r"metricas_(\d{4}-\d{2})(?:__(\d{3}))?\.csv", name)
    if not match:
        raise FileContractError("Expected metricas_YYYY-MM.csv or metricas_YYYY-MM__NNN.csv")
    try:
        month = date.fromisoformat(match[1] + "-01")
    except ValueError as exc:
        raise FileContractError("Invalid delivery month") from exc
    return month, int(match[2] or 0)


def read_csv(content: bytes, expected=None):
    try:
        text = content.decode("utf-8-sig")
        header = text.splitlines()[0]
        delimiter = ";" if ";" in header else ","
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        fields = tuple(reader.fieldnames or ())
        if expected is not None and fields != expected:
            raise FileContractError("Unexpected master header")
        rows = []
        for raw in reader:
            rows.append((reader.line_num, raw))
        return fields, rows, delimiter
    except (UnicodeDecodeError, csv.Error, IndexError) as exc:
        raise FileContractError(f"Invalid UTF-8 CSV: {exc}") from exc


def parse_master(content: bytes) -> dict[int, dict]:
    _, rows, _ = read_csv(content, MASTER)
    firms = {}
    try:
        for _, row in rows:
            if None in row or any(v is None or not v.strip() for v in row.values()):
                raise ValueError("Missing master value")
            item = {k: v.strip() for k, v in row.items()}
            item["id"] = int(item["id"])
            item["num_empleados"] = int(item["num_empleados"])
            item["fecha_alta"] = date.fromisoformat(item["fecha_alta"])
            if item["id"] in firms or item["id"] <= 0 or item["num_empleados"] < 0:
                raise ValueError("Duplicate ID or invalid master number")
            firms[item["id"]] = item
    except (ValueError, TypeError) as exc:
        raise FileContractError(f"Invalid master: {exc}") from exc
    if not firms:
        raise FileContractError("Empty master")
    return firms


def parse_drop(content: bytes, name: str, firms: dict[int, dict]):
    delivered, _ = delivery_key(name)
    header, source, delimiter = read_csv(content)
    if header not in (V1, V2):
        raise FileContractError("Unknown header: schema changes require an explicit adapter")
    version = 1 if header == V1 else 2
    if delimiter != ("," if version == 1 else ";"):
        raise FileContractError("Unexpected delimiter for schema version")
    parsed = []
    for line, raw in source:
        row = ParsedRow(line=line, raw=raw)
        parsed.append(row)
        if None in raw or any(value is None for value in raw.values()):
            row.issue("column_count", "Row has a different number of columns than its header")
            continue
        clean = {k: v.strip() for k, v in raw.items()}
        try:
            if not re.fullmatch(r"\d+", clean["asesoria_id"]):
                raise ValueError()
            row.values["asesoria_id"] = int(clean["asesoria_id"])
        except ValueError:
            row.issue("invalid_id", "ID must be a positive integer", "asesoria_id")
        firm = firms.get(row.values.get("asesoria_id"))
        if firm is None:
            row.issue("unknown_firm", "ID does not exist in the master", "asesoria_id")
        month = None
        try:
            if not re.fullmatch(r"\d{4}-\d{2}", clean["mes"]):
                raise ValueError()
            month = date.fromisoformat(clean["mes"] + "-01")
            row.values["mes"] = month
            if month > delivered:
                row.issue("future_month", "Metric month is after delivery month", "mes")
            if firm and month < firm["fecha_alta"].replace(day=1):
                row.issue("before_registration", "Metric month predates firm registration", "mes")
        except ValueError:
            row.issue("invalid_month", "Month must be YYYY-MM", "mes")
        if version == 2:
            clean["satisfaccion_cliente"] = clean.pop("satisfaccion_media")
        for column in METRICS:
            value = clean.get(column, "")
            if column == "facturacion_otros_eur" and not value and month and month < CHANGEOVER:
                # This category did not exist in the old reporting contract.
                row.values[column] = Decimal("0.00")
                continue
            if not value:
                row.values[column] = None
                row.issue(
                    "missing_value",
                    "Blank retained as unknown; never imputed to zero",
                    column,
                    "warning" if column in OPTIONAL else "error",
                )
                continue
            try:
                pattern = r"-?\d+(?:\.\d+)?" if version == 1 else r"-?\d+(?:,\d+)?"
                if not re.fullmatch(pattern, value):
                    raise InvalidOperation()
                number = Decimal(value.replace(",", "."))
                if column in COUNTS:
                    if number != number.to_integral_value() or not 0 <= number <= 2147483647:
                        raise InvalidOperation()
                    row.values[column] = int(number)
                else:
                    if number < 0 or number >= Decimal("10000000000000000"):
                        raise InvalidOperation()
                    if number != number.quantize(Decimal("0.01")):
                        raise InvalidOperation()
                    row.values[column] = number
                    if column == "satisfaccion_cliente" and not 1 <= number <= 5:
                        row.issue(
                            "satisfaction_range", "Satisfaction must be between 1 and 5", column
                        )
            except InvalidOperation:
                row.issue(
                    "invalid_number", "Expected a nonnegative number with valid precision", column
                )
        received = row.values.get("consultas_recibidas")
        resolved = row.values.get("consultas_resueltas")
        if received is not None and resolved is not None and resolved > received:
            row.issue(
                "backlog_resolved",
                "Resolutions may include earlier backlog; retained",
                "consultas_resueltas",
                "warning",
            )
    # Only valid candidates compete. Conflicting valid duplicates have no ordering guarantee.
    groups = defaultdict(list)
    for row in parsed:
        if row.status == "accepted":
            groups[(row.values["asesoria_id"], row.values["mes"])].append(row)
    for group in groups.values():
        if any(row.values != group[0].values for row in group[1:]):
            for row in group:
                row.issue(
                    "conflicting_duplicate", "Conflicting valid rows in one delivery; quarantined"
                )
        else:
            for row in group[1:]:
                row.status = "duplicate"
                row.issue(
                    "exact_duplicate",
                    f"Identical to row {group[0].line}; counted once",
                    severity="info",
                )
    return version, parsed


def issue_dicts(row):
    return [asdict(issue) for issue in row.issues]
