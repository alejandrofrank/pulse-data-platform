#!/usr/bin/env python3
"""Generate entirely synthetic advisory CSVs using only the Python standard library.

Usage (the output argument is the data directory itself)::

    python generate_demo_data.py --output data
    python generate_demo_data.py --output data --check

Compatible with placement in backend/03_scripts. No production imports, services,
source data, environment variables, clock values, or network access are used.
The fixed scenario schedule is independent of the seed; changing --seed changes
identities and measurements while retaining the same quality demonstration cases.
--check compares expected file bytes and the CSV inventory without writing.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

DEFAULT_SEED = 731942
GENERATOR_VERSION = 1
FIRM_COUNT = 500
MONTHS = tuple(f"{n // 12:04d}-{n % 12 + 1:02d}" for n in range(2023 * 12 + 3, 2026 * 12 + 3))
CHANGEOVER = "2025-11"
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
OPTIONAL = ("horas_trabajadas", "documentos_procesados", "satisfaccion_cliente")
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


def _rng(seed: int, domain: str, *parts: object) -> random.Random:
    """Separate streams keep identity, measurements and corrections independent."""
    material = json.dumps([seed, domain, *parts], separators=(",", ":")).encode("utf-8")
    return random.Random(int.from_bytes(hashlib.sha256(material).digest(), "big"))


def _word(rng: random.Random) -> str:
    syllables = ("za", "vo", "li", "nu", "pe", "ka", "mi", "ro", "te", "su", "fe", "do")
    return "".join(rng.choice(syllables) for _ in range(4)).capitalize()


def _fixed(cents: int, separator: str = ".") -> str:
    sign = "-" if cents < 0 else ""
    whole, fractional = divmod(abs(cents), 100)
    return f"{sign}{whole}{separator}{fractional:02d}"


def _identity(firm_id: int, month: str) -> dict:
    return {"asesoria_id": firm_id, "mes": month}


def _issue(code: str, severity: str, column: str | None = None) -> dict:
    return {"code": code, "severity": severity, "field": column}


@dataclass
class PlannedRow:
    values: dict
    status: str = "accepted"
    issues: list[dict] = field(default_factory=list)
    scenario: str | None = None
    details: dict = field(default_factory=dict)


def _masters(seed: int) -> tuple[list[dict], dict[int, dict]]:
    region_rng = _rng(seed, "locations")
    regions = [f"DEMO Region {i:02d} {_word(region_rng)}" for i in range(1, 21)]
    cities = [f"DEMO Villa {i:03d} {_word(region_rng)}" for i in range(1, 81)]
    rows, profiles = [], {}
    for firm_id in range(1, FIRM_COUNT + 1):
        rng = _rng(seed, "identity", firm_id)
        employees = rng.randint(3, 44)
        token = f"{rng.getrandbits(48):012X}"
        rows.append(
            {
                "id": str(firm_id),
                "nombre": f"DEMO Asesoria {firm_id:04d} {_word(rng)}",
                "cif": f"DEMO-{firm_id:04d}-{token}",
                "provincia": rng.choice(regions),
                "ciudad": rng.choice(cities),
                "fecha_alta": (date(2017, 1, 1) + timedelta(days=rng.randrange(2100))).isoformat(),
                "num_empleados": str(employees),
                "especialidad": rng.choice(
                    ("Fiscal", "Laboral", "Contable", "Consultoria", "Integral")
                ),
            }
        )
        profile_rng = _rng(seed, "firm-profile", firm_id)
        profiles[firm_id] = {
            "employees": employees,
            "clients": employees * profile_rng.randint(6, 14) + profile_rng.randint(12, 85),
            "growth": profile_rng.randint(-2, 9),
            "fee_cents": profile_rng.randint(6500, 22500),
            "satisfaction": profile_rng.randint(325, 468),
        }
    return rows, profiles


def _metrics(seed: int, firm_id: int, index: int, profile: dict) -> dict:
    rng = _rng(seed, "firm-month", firm_id, MONTHS[index])
    calendar_month = int(MONTHS[index][5:])
    clients = max(18, profile["clients"] + index * profile["growth"] // 3 + rng.randint(-9, 9))
    received = clients * rng.randint(2, 5) // 3 + rng.randint(4, 29)
    income_tax = (
        clients * rng.randint(35, 85) // 100 if calendar_month in (4, 5, 6) else rng.randint(0, 8)
    )
    vat = clients * rng.randint(55, 90) // 100 if calendar_month in (1, 4, 7, 10) else clients // 12
    corporate = clients // 3 + rng.randint(3, 23) if calendar_month == 7 else rng.randint(0, 6)
    money = clients * profile["fee_cents"] + rng.randint(10000, 99000) + index * 113
    return {
        **_identity(firm_id, MONTHS[index]),
        "clientes_activos": clients,
        "clientes_nuevos": rng.randint(0, max(2, clients // 19)),
        "clientes_baja": rng.randint(0, max(2, clients // 33)),
        "declaraciones_renta": income_tax,
        "declaraciones_iva": vat,
        "declaraciones_sociedades": corporate,
        "declaraciones_otros": rng.randint(2, 48),
        "facturacion_asesoria_eur": money,
        "facturacion_gestion_eur": clients * rng.randint(900, 3300) + vat * 137,
        "facturacion_consultoria_eur": rng.randint(11000, 970000) + corporate * 739,
        "facturacion_otros_eur": rng.randint(4500, 270000) if MONTHS[index] >= CHANGEOVER else 0,
        "horas_trabajadas": profile["employees"] * rng.randint(12600, 16900) + income_tax * 25,
        "consultas_recibidas": received,
        "consultas_resueltas": max(0, received - rng.randint(0, min(19, received))),
        "incidencias_aeat": rng.randint(0, max(1, clients // 31)),
        "documentos_procesados": clients * rng.randint(5, 17) + income_tax + vat,
        "satisfaccion_cliente": min(499, max(280, profile["satisfaction"] + rng.randint(-27, 27))),
    }


def _csv_bytes(header: tuple, rows: list[dict], delimiter: str = ",") -> bytes:
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=header, delimiter=delimiter, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")


def _format_row(values: dict, version: int) -> dict:
    record = {}
    for output_column in V1 if version == 1 else V2:
        column = "satisfaccion_cliente" if output_column == "satisfaccion_media" else output_column
        value = values[column]
        if value is None or (column == "facturacion_otros_eur" and values["mes"] < CHANGEOVER):
            text = ""
        elif column in REVENUE or column in ("horas_trabajadas", "satisfaccion_cliente"):
            text = _fixed(value, "." if version == 1 else ",")
        else:
            text = str(value)
        record[output_column] = text
    return record


def build_dataset(seed: int = DEFAULT_SEED) -> dict[str, bytes]:
    """Return relative output paths and bytes, including a reproducible manifest.

    Expectations come from the explicit generation plan, never from parsing CSVs.
    Monetary values in the internal plan are integer cents, including corrections.
    """
    master, profiles = _masters(seed)
    gap_keys = {(11 + 37 * i, MONTHS[1 + 3 * i]) for i in range(12)}
    optional_keys = {(53 + 11 * i, MONTHS[(31 + 5 * i) % 36]): OPTIONAL[i % 3] for i in range(24)}
    backlog_keys = {(67 + 13 * i, MONTHS[(32 + 7 * i) % 36]) for i in range(18)}
    correction_keys = [(23 + 7 * i, 30 if i == 0 else (7 * i) % 35) for i in range(40)]
    duplicate_keys = [(79 + 9 * i, (31 + i) % 36) for i in range(31)]
    warning_keys = set(optional_keys) | backlog_keys
    assert not (set(optional_keys) & backlog_keys)
    assert not (gap_keys & warning_keys)
    for schedule in (correction_keys, duplicate_keys):
        assert not (
            {(firm_id, MONTHS[index]) for firm_id, index in schedule} & (gap_keys | warning_keys)
        )

    deliveries: dict[str, list[PlannedRow]] = {month: [] for month in MONTHS}
    originals: dict[tuple[int, str], PlannedRow] = {}
    winners: dict[tuple[int, str], dict] = {}
    for index, month in enumerate(MONTHS):
        for firm_id in range(1, FIRM_COUNT + 1):
            key = firm_id, month
            if key in gap_keys:
                continue
            values = _metrics(seed, firm_id, index, profiles[firm_id])
            row = PlannedRow(values)
            if key in optional_keys:
                column = optional_keys[key]
                row.values[column] = None
                row.issues = [_issue("missing_value", "warning", column)]
                row.scenario = "blank_optional"
            if key in backlog_keys:
                row.values["consultas_resueltas"] = values["consultas_recibidas"] + _rng(
                    seed, "backlog", firm_id, month
                ).randint(3, 28)
                row.issues = [_issue("backlog_resolved", "warning", "consultas_resueltas")]
                row.scenario = "backlog_resolved"
            originals[key] = row
            winners[key] = values
            deliveries[month].append(row)

    for firm_id, index in correction_keys:
        month, delivered = MONTHS[index], MONTHS[index + 1]
        original = originals[firm_id, month]
        values = dict(original.values)
        delta = _rng(seed, "correction", firm_id, month).randint(25000, 95000)
        column = "facturacion_asesoria_eur"
        values[column] += delta
        deliveries[delivered].append(
            PlannedRow(
                values,
                scenario="correction",
                details={
                    "original_file": f"drops/metricas_{month}.csv",
                    "changed_fields": {
                        column: {
                            "before": _fixed(original.values[column]),
                            "after": _fixed(values[column]),
                        }
                    },
                },
            )
        )
        winners[firm_id, month] = values

    for firm_id, index in duplicate_keys:
        original = originals[firm_id, MONTHS[index]]
        deliveries[MONTHS[index]].append(
            PlannedRow(
                dict(original.values),
                "duplicate",
                [_issue("exact_duplicate", "info")],
                "exact_duplicate",
            )
        )

    # Invalid candidates are added to later deliveries, never substituted for a
    # valid row. Each has exactly one planned error and no incidental warnings.
    for kind in ("unknown_id", "invalid_month", "negative_money", "out_of_range_satisfaction"):
        for i in range(7):
            delivered_index = 31 + i % 5
            index = delivered_index - 1
            firm_id = {
                "unknown_id": 300 + i,
                "invalid_month": 320 + i,
                "negative_money": 37 + 43 * i,
                "out_of_range_satisfaction": 41 + 47 * i,
            }[kind]
            key = firm_id, MONTHS[index]
            original = originals[key]
            assert not original.issues
            values = dict(original.values)
            details = {}
            if kind == "unknown_id":
                values["asesoria_id"] = 8001 + i
                issue = _issue("unknown_firm", "error", "asesoria_id")
            elif kind == "invalid_month":
                values["mes"] = MONTHS[delivered_index][:4] + "-13"
                issue = _issue("invalid_month", "error", "mes")
            elif kind == "negative_money":
                values["facturacion_gestion_eur"] = -_rng(seed, "negative-money", i).randint(
                    1250, 85000
                )
                issue = _issue("invalid_number", "error", "facturacion_gestion_eur")
                details["retained_key"] = _identity(*key)
            else:
                values["satisfaccion_cliente"] = _rng(seed, "bad-satisfaction", i).randint(601, 935)
                issue = _issue("satisfaction_range", "error", "satisfaccion_cliente")
                details["retained_key"] = _identity(*key)
            deliveries[MONTHS[delivered_index]].append(
                PlannedRow(values, "rejected", [issue], kind, details)
            )

    files = {"asesorias.csv": _csv_bytes(MASTER, master)}
    inventory = {"asesorias.csv": {"rows": len(master), "schema": "master", "delimiter": ","}}
    statuses, issues, severities = Counter(), Counter(), Counter()
    corrections, issue_scenarios = [], []
    references = {}
    warning_rows = 0
    for month, rows in deliveries.items():
        version = 1 if month < CHANGEOVER else 2
        path = f"drops/metricas_{month}.csv"
        files[path] = _csv_bytes(
            V1 if version == 1 else V2,
            [_format_row(row.values, version) for row in rows],
            "," if version == 1 else ";",
        )
        inventory[path] = {
            "rows": len(rows),
            "schema": f"v{version}",
            "delimiter": "," if version == 1 else ";",
            "expected_status_counts": {
                status: sum(row.status == status for row in rows)
                for status in ("accepted", "duplicate", "rejected")
            },
        }
        for line, row in enumerate(rows, start=2):
            statuses[row.status] += 1
            issues.update(issue["code"] for issue in row.issues)
            severities.update(issue["severity"] for issue in row.issues)
            warning_rows += any(issue["severity"] == "warning" for issue in row.issues)
            key = row.values["asesoria_id"], row.values["mes"]
            ref = {**_identity(*key), "file": path, "line": line}
            if row.status == "accepted" and row.scenario != "correction":
                references[key] = ref
            if row.scenario:
                entry = {
                    "kind": row.scenario,
                    **ref,
                    "expected_status": row.status,
                    "issues": row.issues,
                    **row.details,
                }
                if row.scenario == "correction":
                    entry["original_line"] = references[key]["line"]
                    corrections.append(entry)
                else:
                    issue_scenarios.append(entry)

    # The named first correction is deliberately the October -> November
    # schema transition, regardless of delivery order in the evidence list.
    corrections.sort(key=lambda item: (item["asesoria_id"], item["mes"]))
    gaps = [_identity(*key) for key in sorted(gap_keys)]
    totals = {}
    for month in MONTHS:
        selected = [values for (_, key_month), values in winners.items() if key_month == month]
        totals[month] = {
            "revenue_eur": _fixed(sum(sum(row[column] for column in REVENUE) for row in selected)),
            "clientes_activos": sum(row["clientes_activos"] for row in selected),
            "reporting_firms": len(selected),
        }
    fallback_keys = {
        tuple(entry["retained_key"].values())
        for entry in issue_scenarios
        if "retained_key" in entry
    }
    investigation = references[42, "2024-01"]
    assert (
        sum(
            (row.values["asesoria_id"], row.values["mes"]) == (42, "2024-01")
            for rows in deliveries.values()
            for row in rows
        )
        == 1
    )
    assert statuses == {"accepted": 18028, "duplicate": 31, "rejected": 28}
    assert len(winners) == 17988 and len(fallback_keys) == 14
    for path, metadata in inventory.items():
        metadata["sha256"] = hashlib.sha256(files[path]).hexdigest()
        metadata["bytes"] = len(files[path])
    manifest = {
        "manifest_version": 1,
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "synthetic": True,
        "provenance": (
            "Entirely procedural fictional identities, locations and measurements; "
            "no source records used. DEMO tax IDs are intentionally non-real."
        ),
        "period": {
            "start": MONTHS[0],
            "end": MONTHS[-1],
            "months": list(MONTHS),
            "format_change": CHANGEOVER,
        },
        "counts": {
            "firms": FIRM_COUNT,
            "months": len(MONTHS),
            "monthly_files": len(deliveries),
            "csv_files": len(files),
            "expected_firm_months": FIRM_COUNT * len(MONTHS),
            "base_rows": len(originals),
            "correction_rows": len(corrections),
        },
        "expected_quality": {
            "raw_rows": sum(statuses.values()),
            "accepted_rows": statuses["accepted"],
            "duplicate_rows": statuses["duplicate"],
            "rejected_rows": statuses["rejected"],
            "curated_rows": len(winners),
            "published_keys": len(winners),
            "missing_firm_months": len(gaps),
            "fallback_rows": len(fallback_keys),
            "warning_rows": warning_rows,
            "issue_counts": dict(sorted(issues.items())),
            "severity_counts": dict(sorted(severities.items())),
            "optional_nulls_in_publication": {
                column: sum(row[column] is None for row in winners.values()) for column in OPTIONAL
            },
        },
        "monthly_totals": totals,
        "scenarios": {
            "first_correction": corrections[0],
            "first_gap": gaps[0],
            "investigation": investigation,
            **{
                kind: next(entry for entry in issue_scenarios if entry["kind"] == kind)
                for kind in (
                    "blank_optional",
                    "backlog_resolved",
                    "exact_duplicate",
                    "unknown_id",
                    "invalid_month",
                    "negative_money",
                    "out_of_range_satisfaction",
                )
            },
        },
        "gaps": gaps,
        "gap_examples": gaps[:3],
        "corrections": corrections,
        "issue_scenarios": issue_scenarios,
        "schemas": {
            "master": list(MASTER),
            "v1": list(V1),
            "v2": list(V2),
            "encoding": "utf-8",
            "line_ending": "LF",
            "v1_decimal_separator": ".",
            "v2_decimal_separator": ",",
            "schema_selected_by": "delivery month",
            "legacy_other_revenue": (
                "absent in v1; blank in v2 corrections for months before 2025-11 "
                "means structural zero"
            ),
        },
        "count_contract": {
            "raw_rows": "Metric CSV data rows only; excludes headers and the 500 master rows.",
            "accepted_rows": (
                "Includes 40 later-delivery corrections; excludes duplicate and rejected rows."
            ),
            "curated_rows": (
                "One latest accepted delivery per firm/month; "
                "rejected newer candidates never replace it."
            ),
            "monthly_totals": (
                "Computed directly from planned valid winners with integer cents, "
                "before CSV serialization; includes corrections."
            ),
            "expected_quality": (
                "Derived from explicit generation annotations, "
                "independently of any production parser."
            ),
            "line": "One-based physical CSV line; the header is line 1.",
            "checksums": (
                "SHA-256 of exact CSV file bytes. Paths are relative to the output data directory. "
                "Manifest excludes itself to avoid recursive hashing."
            ),
        },
        "files": inventory,
    }
    files["manifest.json"] = (
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    return files


def check_output(output: Path, expected: dict[str, bytes]) -> list[str]:
    """Read-only exact byte comparison; report missing, changed and stale CSVs."""
    differences = []
    for relative, content in sorted(expected.items()):
        path = output / relative
        if not path.is_file():
            differences.append(f"missing: {relative}")
        elif path.read_bytes() != content:
            differences.append(f"different: {relative}")
    if output.is_dir():
        differences.extend(
            f"unexpected CSV: {path.relative_to(output).as_posix()}"
            for path in sorted(output.rglob("*.csv"))
            if path.relative_to(output).as_posix() not in expected
        )
    return differences


def write_output(output: Path, expected: dict[str, bytes]) -> None:
    """Write generated files; refuse stale CSVs rather than silently retain them."""
    if output.is_dir():
        extras = [
            path.relative_to(output).as_posix()
            for path in output.rglob("*.csv")
            if path.relative_to(output).as_posix() not in expected
        ]
        if extras:
            raise ValueError(
                "Unexpected CSV files in output directory: " + ", ".join(sorted(extras))
            )
    for relative, content in sorted(expected.items()):
        path = output / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--output", type=Path, default=Path("data"), help="Data directory itself (default: ./data)"
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"Independent synthetic seed (default: {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare bytes only; create, overwrite and delete nothing",
    )
    args = parser.parse_args(argv)
    try:
        expected = build_dataset(args.seed)
        if args.check:
            differences = check_output(args.output, expected)
            if differences:
                print("Synthetic data check failed:\n" + "\n".join(differences), file=sys.stderr)
                return 1
            print(f"OK: {len(expected)} files match byte-for-byte in {args.output}")
        else:
            write_output(args.output, expected)
            print(
                f"Generated {len(expected)} files in {args.output}: "
                "500 firms, 18087 raw rows, 17988 published keys"
            )
        return 0
    except (OSError, ValueError) as exc:
        print(f"Synthetic data error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
