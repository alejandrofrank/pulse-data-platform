"""Pure policy examples; neither fixture construction nor assertions use the parser."""

import csv
import io
from datetime import date
from decimal import Decimal, localcontext

import pytest

from pulse_api.reconciliation import VERIFIER_VERSION, VerificationLimit, verify_delivery

HEADER = (
    "asesoria_id,mes,clientes_activos,clientes_nuevos,clientes_baja,declaraciones_renta,"
    "declaraciones_iva,declaraciones_sociedades,declaraciones_otros,facturacion_asesoria_eur,"
    "facturacion_gestion_eur,facturacion_consultoria_eur,horas_trabajadas,consultas_recibidas,"
    "consultas_resueltas,incidencias_aeat,documentos_procesados,satisfaccion_cliente"
).split(",")
FIRMS = {1: {"fecha_alta": date(2020, 5, 20)}}


def delivery(*changes, version=1, delimiter=None):
    fields = list(HEADER)
    if version == 2:
        fields.insert(12, "facturacion_otros_eur")
        fields[-1] = "satisfaccion_media"
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fields, delimiter=delimiter or ("," if version == 1 else ";"))
    writer.writeheader()
    for change in changes:
        row = dict.fromkeys(fields, "10")
        row.update(asesoria_id="1", mes="2025-10")
        row[fields[-1]] = "4"
        if version == 2:
            row["facturacion_otros_eur"] = ""
        row.update(change)
        writer.writerow(row)
    return output.getvalue().encode("utf-8")


def verify(content, filename="metricas_2025-11.csv", firms=None):
    return verify_delivery(content, filename, FIRMS if firms is None else firms)


def one(change, version=1):
    return verify(delivery(change, version=version))["records"][2]


def test_formats_and_old_month_other_revenue():
    old = verify(delivery({"facturacion_asesoria_eur": " 120.25 "}))
    new = verify(b"\xef\xbb\xbf" + delivery({"facturacion_asesoria_eur": "120,25"}, version=2))
    assert old["schema_version"] == 1 and new["schema_version"] == 2
    left, right = old["records"][2], new["records"][2]
    assert left["normalized"] == right["normalized"]
    assert left["status"] == right["status"] == "accepted"
    assert left["payload"]["facturacion_asesoria_eur"] == " 120.25 "
    assert left["normalized"]["facturacion_otros_eur"] == Decimal("0.00")
    assert type(left["normalized"]["clientes_activos"]) is int
    assert type(left["normalized"]["mes"]) is date
    assert type(left["normalized"]["facturacion_asesoria_eur"]) is Decimal
    assert VERIFIER_VERSION


@pytest.mark.parametrize("version", [1, 2])
def test_new_month_requires_other_revenue(version):
    result = one({"mes": "2025-11"}, version)
    assert result["status"] == "rejected"
    assert result["normalized"]["facturacion_otros_eur"] is None
    assert result["issue_codes"] == ["missing_value"]


def test_reported_other_revenue_not_erased_for_old_month():
    assert one({"facturacion_otros_eur": "12,30"}, 2)["normalized"][
        "facturacion_otros_eur"
    ] == Decimal("12.30")


@pytest.mark.parametrize(
    "field",
    [
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
    ],
)
@pytest.mark.parametrize("value", ["-1", "1.5", "2147483648"])
def test_all_counts_reject_invalid_values(field, value):
    result = one({field: value})
    assert result["status"] == "rejected"
    assert "invalid_number" in result["issue_codes"]
    assert field not in result["normalized"]


@pytest.mark.parametrize(
    "field",
    [
        "facturacion_asesoria_eur",
        "facturacion_gestion_eur",
        "facturacion_consultoria_eur",
        "facturacion_otros_eur",
        "horas_trabajadas",
    ],
)
@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "1e2", "1,001", "10000000000000000"])
def test_money_and_hours_validation(field, value):
    result = one({field: value}, 2)
    assert result["status"] == "rejected"
    assert field not in result["normalized"]
    assert "invalid_number" in result["issue_codes"]


@pytest.mark.parametrize("value", ["0", "5.01", "99"])
def test_satisfaction_range_preserves_parsed_value(value):
    result = one({"satisfaccion_cliente": value})
    assert result["status"] == "rejected"
    assert result["normalized"]["satisfaccion_cliente"] == Decimal(value)
    assert result["issue_codes"] == ["satisfaction_range"]


@pytest.mark.parametrize(
    "change,code",
    [
        ({"asesoria_id": "-1"}, "invalid_id"),
        ({"asesoria_id": "x"}, "invalid_id"),
        ({"asesoria_id": "0"}, "unknown_firm"),
        ({"asesoria_id": "999"}, "unknown_firm"),
        ({"mes": "2025-13"}, "invalid_month"),
        ({"mes": "2025-1"}, "invalid_month"),
        ({"mes": "2025-12"}, "future_month"),
        ({"mes": "2020-04"}, "before_registration"),
        ({"facturacion_gestion_eur": ""}, "missing_value"),
        ({"clientes_activos": ""}, "missing_value"),
    ],
)
def test_keys_and_required_fields(change, code):
    result = one(change)
    assert result["status"] == "rejected"
    assert code in result["issue_codes"]


def test_registration_month_and_id_normalization():
    result = one({"asesoria_id": " 001 ", "mes": "2020-05"})
    assert result["status"] == "accepted"
    assert result["normalized"]["asesoria_id"] == 1


def test_optional_nulls_and_backlog_warnings_remain_eligible():
    result = one(
        {
            "horas_trabajadas": "",
            "documentos_procesados": " ",
            "satisfaccion_cliente": "",
            "consultas_resueltas": "11",
        }
    )
    assert result["status"] == "accepted"
    assert result["issue_codes"] == ["missing_value"] * 3 + ["backlog_resolved"]
    for field in ("horas_trabajadas", "documentos_procesados", "satisfaccion_cliente"):
        assert result["normalized"][field] is None


def test_column_count_payload_and_early_rejection():
    content = delivery({}) + b"1,2025-10\r\n" + delivery({}).split(b"\r\n")[1] + b",extra\r\n"
    records = verify(content)["records"]
    for line in (3, 4):
        assert records[line]["normalized"] == {}
        assert records[line]["status"] == "rejected"
        assert records[line]["issue_codes"] == ["column_count"]
    assert records[3]["payload"]["clientes_activos"] is None
    assert records[4]["payload"][None] == ["extra"]


def test_quoting_multiline_blank_lines_and_ending_physical_line():
    content = delivery({"horas_trabajadas": "\r\n10\r\n"}, {"horas_trabajadas": 'a,"b"'})
    content += b"\r\n" + delivery({}).split(b"\r\n")[1] + b"\r\n"
    records = verify(content)["records"]
    assert list(records) == [4, 5, 7]
    assert records[4]["payload"]["horas_trabajadas"] == "\r\n10\r\n"
    assert records[4]["normalized"]["horas_trabajadas"] == Decimal(10)
    assert records[5]["payload"]["horas_trabajadas"] == 'a,"b"'
    assert records[5]["status"] == "rejected"
    assert records[7]["status"] == "duplicate"


def test_identical_normalized_duplicates_ignore_invalid_candidate():
    records = verify(
        delivery({}, {"facturacion_asesoria_eur": "10.000"}, {"satisfaccion_cliente": "99"})
    )["records"]
    assert [row["status"] for row in records.values()] == ["accepted", "duplicate", "rejected"]
    assert records[3]["issue_codes"] == ["exact_duplicate"]


@pytest.mark.parametrize(
    "change", [{"clientes_activos": "11"}, {"horas_trabajadas": ""}, {"satisfaccion_cliente": "3"}]
)
def test_full_row_conflicts_reject_all_valid_candidates(change):
    records = verify(delivery({}, {}, change))["records"]
    assert all(row["status"] == "rejected" for row in records.values())
    assert all(row["issue_codes"][-1] == "conflicting_duplicate" for row in records.values())


def test_exact_boundaries_do_not_depend_on_decimal_context():
    with localcontext() as context:
        context.prec = 3
        result = one(
            {
                "clientes_activos": "2147483647.000",
                "facturacion_asesoria_eur": "9999999999999999.99",
                "facturacion_gestion_eur": "-0.000",
            }
        )
    assert result["status"] == "accepted"
    assert result["normalized"]["facturacion_asesoria_eur"] == Decimal("9999999999999999.99")
    assert one({"facturacion_asesoria_eur": "0.001"})["status"] == "rejected"


@pytest.mark.parametrize(
    "filename",
    ["x.csv", "metricas_2025-13.csv", "metricas_2025-11__1.csv", "../metricas_2025-11.csv"],
)
def test_invalid_filename(filename):
    with pytest.raises(ValueError):
        verify(delivery({}), filename)


def test_numbered_revision_and_header_only():
    assert verify(delivery({}), "metricas_2025-11__001.csv")["records"][2]["status"] == "accepted"
    assert verify(delivery())["records"] == {}


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\xff",
        b"bad,header\n1,2\n",
        delivery({}, delimiter=";"),
        delivery({}, version=2, delimiter=","),
        delivery() + b'"unterminated',
    ],
)
def test_unsupported_or_unreadable_file(content):
    with pytest.raises(ValueError):
        verify(content)


def test_small_record_limit_stops_before_normalizing_excess_or_reading_tail(monkeypatch):
    from pulse_api import reconciliation

    original = reconciliation._normalize
    calls = []

    def tracked(*args):
        calls.append(args[0])
        return original(*args)

    monkeypatch.setattr(reconciliation, "_normalize", tracked)
    content = delivery({}, {"satisfaccion_cliente": "99"}, {}) + b'"unterminated'
    with pytest.raises(VerificationLimit, match="limit of 2 records"):
        verify_delivery(content, "metricas_2025-11.csv", FIRMS, max_records=2)
    assert len(calls) == 2


def test_record_limit_counts_duplicates_not_blank_or_multiline_physical_lines():
    content = delivery({"horas_trabajadas": "\r\n10\r\n"}, {}) + b"\r\n\r\n"
    result = verify_delivery(content, "metricas_2025-11.csv", FIRMS, max_records=2)
    assert list(result["records"]) == [4, 5]
    assert result["records"][5]["status"] == "duplicate"
    with pytest.raises(VerificationLimit):
        verify_delivery(content, "metricas_2025-11.csv", FIRMS, max_records=1)


def test_zero_record_budget_allows_only_header():
    assert (
        verify_delivery(delivery(), "metricas_2025-11.csv", FIRMS, max_records=0)["records"] == {}
    )
    with pytest.raises(VerificationLimit):
        verify_delivery(delivery({}), "metricas_2025-11.csv", FIRMS, max_records=0)


@pytest.mark.parametrize("max_records", [-1, None, True, 1.5])
def test_invalid_record_budget(max_records):
    with pytest.raises(ValueError, match="nonnegative integer") as exc:
        verify_delivery(delivery(), "metricas_2025-11.csv", FIRMS, max_records=max_records)
    assert not isinstance(exc.value, VerificationLimit)
