import csv
import io
from datetime import date
from decimal import Decimal

import pytest

from pulse.parsing import V1, V2, FileContractError, delivery_key, parse_drop

FIRMS = {1: {"fecha_alta": date(2020, 1, 1)}}


def make_csv(rows, version=1):
    out = io.StringIO()
    writer = csv.DictWriter(
        out, fieldnames=V1 if version == 1 else V2, delimiter="," if version == 1 else ";"
    )
    writer.writeheader()
    for overrides in rows:
        row = dict.fromkeys(writer.fieldnames, "10")
        row.update(asesoria_id="1", mes="2025-10")
        row["satisfaccion_cliente" if version == 1 else "satisfaccion_media"] = "4"
        row.update(overrides)
        writer.writerow(row)
    return out.getvalue().encode()


def test_both_formats_and_legacy_correction():
    old = make_csv([{"facturacion_asesoria_eur": "120.25"}])
    new = make_csv([{"facturacion_asesoria_eur": "120,25", "facturacion_otros_eur": ""}], 2)
    _, left = parse_drop(old, "metricas_2025-10.csv", FIRMS)
    _, right = parse_drop(new, "metricas_2025-11.csv", FIRMS)
    assert left[0].values == right[0].values
    assert right[0].values["facturacion_otros_eur"] == Decimal("0")


def test_missing_optional_and_backlog_are_not_rejected():
    data = make_csv([{"horas_trabajadas": "", "consultas_resueltas": "50"}])
    _, rows = parse_drop(data, "metricas_2025-10.csv", FIRMS)
    assert rows[0].status == "accepted"
    assert rows[0].values["horas_trabajadas"] is None
    assert {i.code for i in rows[0].issues} == {"missing_value", "backlog_resolved"}


@pytest.mark.parametrize(
    "change",
    [
        {"asesoria_id": "999"},
        {"mes": "2025-13"},
        {"mes": "2025-12"},
        {"clientes_activos": "-1"},
        {"clientes_nuevos": "1.5"},
        {"facturacion_asesoria_eur": "-10"},
        {"facturacion_gestion_eur": ""},
        {"satisfaccion_cliente": "55"},
        {"horas_trabajadas": "NaN"},
        {"facturacion_asesoria_eur": "1.123"},
    ],
)
def test_invalid_values_are_rejected(change):
    _, rows = parse_drop(make_csv([change]), "metricas_2025-10.csv", FIRMS)
    assert rows[0].status == "rejected"


def test_duplicates_and_invalid_candidate_do_not_replace_valid():
    _, rows = parse_drop(
        make_csv([{}, {}, {"facturacion_asesoria_eur": "-10"}]), "metricas_2025-10.csv", FIRMS
    )
    assert [r.status for r in rows] == ["accepted", "duplicate", "rejected"]


def test_conflicting_valid_duplicates_quarantine_both():
    _, rows = parse_drop(make_csv([{}, {"clientes_activos": "20"}]), "metricas_2025-10.csv", FIRMS)
    assert all(r.status == "rejected" for r in rows)
    assert all(r.issues[-1].code == "conflicting_duplicate" for r in rows)


def test_new_revenue_cannot_silently_default_to_zero():
    _, rows = parse_drop(
        make_csv([{"mes": "2025-11", "facturacion_otros_eur": ""}], 2),
        "metricas_2025-11.csv",
        FIRMS,
    )
    assert rows[0].status == "rejected"


def test_contract_and_filename_validation():
    assert delivery_key("metricas_2025-10__002.csv") == (date(2025, 10, 1), 2)
    for name in ("a.csv", "metricas_2025-13.csv"):
        with pytest.raises(FileContractError):
            delivery_key(name)
    with pytest.raises(FileContractError):
        parse_drop(b"unexpected,column\n1,2\n", "metricas_2025-10.csv", FIRMS)


def test_bad_row_shape_is_a_row_rejection():
    data = make_csv([{}]) + b"1,2025-10\n"
    _, rows = parse_drop(data, "metricas_2025-10.csv", FIRMS)
    assert rows[1].issues[0].code == "column_count"
