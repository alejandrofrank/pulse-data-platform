"""Independent stdlib tests; no production parser, database, pytest or services.

Run from this workspace (or backend after moving the two files)::

    python -B -m unittest discover -s tests -p test_synthetic_data.py -v

Temporary outputs stay beside the generator's project, never in a global temp
directory. The audit below checks actual CSV values independently of generation
annotations; it does not use the generator's schema or quality constants.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = next(
    path
    for path in (ROOT / "generate_demo_data.py", ROOT / "03_scripts" / "generate_demo_data.py")
    if path.is_file()
)
SPEC = importlib.util.spec_from_file_location("synthetic_generator_under_test", SCRIPT)
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)

COUNT_FIELDS = (
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
MONEY_FIELDS = (
    "facturacion_asesoria_eur",
    "facturacion_gestion_eur",
    "facturacion_consultoria_eur",
    "facturacion_otros_eur",
)
NULLABLE = {"horas_trabajadas", "documentos_procesados", "satisfaccion_cliente"}
EXPECTED_MASTER = (
    "id",
    "nombre",
    "cif",
    "provincia",
    "ciudad",
    "fecha_alta",
    "num_empleados",
    "especialidad",
)
EXPECTED_V1 = (
    "asesoria_id",
    "mes",
    *COUNT_FIELDS[:7],
    *MONEY_FIELDS[:3],
    "horas_trabajadas",
    *COUNT_FIELDS[7:],
    "satisfaccion_cliente",
)
EXPECTED_V2 = (
    "asesoria_id",
    "mes",
    *COUNT_FIELDS[:7],
    *MONEY_FIELDS,
    "horas_trabajadas",
    *COUNT_FIELDS[7:],
    "satisfaccion_media",
)
EXPECTED_MONTHS = [
    f"{year:04d}-{month:02d}"
    for year in range(2023, 2027)
    for month in range(1, 13)
    if "2023-04" <= f"{year:04d}-{month:02d}" <= "2026-03"
]


def read_csv(content, delimiter):
    reader = csv.DictReader(
        io.StringIO(content.decode("utf-8"), newline=""), delimiter=delimiter, strict=True
    )
    rows = list(reader)
    assert all(None not in row and None not in row.values() for row in rows)
    return tuple(reader.fieldnames), rows


def audit_csvs(files):
    """Small independent CSV oracle; rejects bad candidates before deduplication."""
    header, master = read_csv(files["asesorias.csv"], ",")
    assert header == EXPECTED_MASTER
    firm_ids = {int(row["id"]) for row in master}
    evidence, winners = [], {}
    all_fields = COUNT_FIELDS + MONEY_FIELDS + ("horas_trabajadas", "satisfaccion_cliente")
    for path in sorted(name for name in files if name.startswith("drops/")):
        delivered = re.fullmatch(r"drops/metricas_(\d{4}-\d{2})\.csv", path).group(1)
        new_format = delivered >= "2025-11"
        header, records = read_csv(files[path], ";" if new_format else ",")
        assert header == (EXPECTED_V2 if new_format else EXPECTED_V1)
        seen = {}
        for line, raw in enumerate(records, 2):
            values = dict(raw)
            if new_format:
                values["satisfaccion_cliente"] = values.pop("satisfaccion_media")
            issues = []
            firm_id = int(values["asesoria_id"])
            month = values["mes"]
            if firm_id not in firm_ids:
                issues.append(("unknown_firm", "error", "asesoria_id"))
            valid_month = False
            try:
                date.fromisoformat(month + "-01")
                assert re.fullmatch(r"\d{4}-\d{2}", month)
                valid_month = True
                assert month <= delivered
            except ValueError:
                issues.append(("invalid_month", "error", "mes"))
            normalized = {"asesoria_id": firm_id, "mes": month}
            for column in all_fields:
                text = values.get(column, "")
                if (
                    column == "facturacion_otros_eur"
                    and not text
                    and valid_month
                    and month < "2025-11"
                ):
                    normalized[column] = Decimal("0.00")
                elif not text:
                    normalized[column] = None
                    issues.append(
                        ("missing_value", "warning" if column in NULLABLE else "error", column)
                    )
                else:
                    assert re.fullmatch(
                        r"-?\d+(?:,\d{1,2})?" if new_format else r"-?\d+(?:\.\d{1,2})?", text
                    )
                    number = Decimal(text.replace(",", "."))
                    normalized[column] = number
                    if number < 0:
                        issues.append(("invalid_number", "error", column))
                    elif column == "satisfaccion_cliente" and not 1 <= number <= 5:
                        issues.append(("satisfaction_range", "error", column))
                    elif column in COUNT_FIELDS:
                        assert number == number.to_integral_value() and number <= 2147483647
                        normalized[column] = int(number)
            if normalized["consultas_resueltas"] > normalized["consultas_recibidas"]:
                issues.append(("backlog_resolved", "warning", "consultas_resueltas"))
            status = (
                "rejected" if any(severity == "error" for _, severity, _ in issues) else "accepted"
            )
            key = firm_id, month
            if status == "accepted":
                if key in seen:
                    assert normalized == seen[key]["values"], "Conflicting valid duplicate"
                    assert raw == seen[key]["raw"], "Duplicate is not an exact raw row copy"
                    status = "duplicate"
                    issues.append(("exact_duplicate", "info", None))
            row = {
                "file": path,
                "line": line,
                "key": key,
                "raw": raw,
                "values": normalized,
                "issues": issues,
                "status": status,
            }
            evidence.append(row)
            if status == "accepted":
                seen[key] = row
                winners[key] = row
    totals = {}
    for month in EXPECTED_MONTHS:
        rows = [row["values"] for (_, key_month), row in winners.items() if key_month == month]
        revenue = sum((sum(row[field] for field in MONEY_FIELDS) for row in rows), Decimal(0))
        totals[month] = {
            "revenue_eur": f"{revenue:.2f}",
            "clientes_activos": sum(row["clientes_activos"] for row in rows),
            "reporting_firms": len(rows),
        }
    return master, evidence, winners, totals


class SyntheticDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = generator.build_dataset()
        cls.manifest = json.loads(cls.files["manifest.json"])
        cls.master, cls.rows, cls.winners, cls.totals = audit_csvs(cls.files)
        cls.by_reference = {(row["file"], row["line"]): row for row in cls.rows}

    def test_reproducible_bytes_and_seed_independence(self):
        self.assertEqual(self.files, generator.build_dataset())
        alternate = generator.build_dataset(generator.DEFAULT_SEED + 1)
        other_master, other_rows, other_winners, _ = audit_csvs(alternate)
        self.assertNotEqual(self.files["asesorias.csv"], alternate["asesorias.csv"])
        for path in self.files:
            if path.startswith("drops/"):
                self.assertNotEqual(self.files[path], alternate[path])
        self.assertEqual(set(self.winners), set(other_winners))
        self.assertEqual(
            Counter(row["status"] for row in self.rows),
            Counter(row["status"] for row in other_rows),
        )
        self.assertNotEqual(self.master[0]["nombre"], other_master[0]["nombre"])
        self.assertNotEqual(self.master[0]["cif"], other_master[0]["cif"])
        self.assertNotEqual(self.master[0]["provincia"], other_master[0]["provincia"])

    def test_master_identities_are_explicitly_fictional_and_all_eligible(self):
        self.assertEqual(len(self.master), 500)
        self.assertEqual({int(row["id"]) for row in self.master}, set(range(1, 501)))
        for column in ("nombre", "cif"):
            self.assertEqual(len({row[column] for row in self.master}), 500)
        for row in self.master:
            for column in ("nombre", "provincia", "ciudad"):
                self.assertTrue(row[column].startswith("DEMO "))
            self.assertRegex(row["cif"], r"^DEMO-\d{4}-[A-F0-9]{12}$")
            self.assertLessEqual(date.fromisoformat(row["fecha_alta"]), date(2023, 4, 1))
            self.assertGreater(int(row["num_empleados"]), 0)

    def test_exact_row_accounting_and_independent_quality(self):
        quality = self.manifest["expected_quality"]
        self.assertEqual(len(self.rows), 18000 - 12 + 40 + 31 + 28)
        self.assertEqual(
            Counter(row["status"] for row in self.rows),
            {"accepted": 18028, "duplicate": 31, "rejected": 28},
        )
        for status in ("accepted", "duplicate", "rejected"):
            self.assertEqual(
                quality[f"{status}_rows"], sum(row["status"] == status for row in self.rows)
            )
        codes = Counter(code for row in self.rows for code, _, _ in row["issues"])
        severities = Counter(severity for row in self.rows for _, severity, _ in row["issues"])
        self.assertEqual(
            codes,
            {
                "unknown_firm": 7,
                "invalid_month": 7,
                "invalid_number": 7,
                "satisfaction_range": 7,
                "missing_value": 24,
                "backlog_resolved": 18,
                "exact_duplicate": 31,
            },
        )
        self.assertEqual(quality["issue_counts"], codes)
        self.assertEqual(quality["severity_counts"], severities)
        self.assertEqual(quality["raw_rows"], len(self.rows))
        self.assertEqual(quality["warning_rows"], 42)
        self.assertEqual(quality["curated_rows"], len(self.winners))
        self.assertEqual(quality["published_keys"], 17988)

    def test_coverage_and_exact_gaps(self):
        expected = {(firm_id, month) for firm_id in range(1, 501) for month in EXPECTED_MONTHS}
        absent = expected - self.winners.keys()
        self.assertEqual(len(absent), 12)
        self.assertEqual(
            absent, {(item["asesoria_id"], item["mes"]) for item in self.manifest["gaps"]}
        )
        self.assertEqual(
            self.manifest["scenarios"]["first_gap"], {"asesoria_id": 11, "mes": "2023-05"}
        )
        for gap in self.manifest["gap_examples"]:
            self.assertIn((gap["asesoria_id"], gap["mes"]), absent)
        self.assertEqual(self.manifest["expected_quality"]["missing_firm_months"], len(absent))
        self.assertEqual(set(month for _, month in self.winners), set(EXPECTED_MONTHS))
        per_firm = Counter(firm_id for firm_id, _ in self.winners)
        self.assertEqual(Counter(per_firm.values()), {35: 12, 36: 488})
        self.assertEqual(len(expected & self.winners.keys()), 17988)
        self.assertFalse(self.winners.keys() - expected)

    def test_format_change_and_cross_format_correction(self):
        drops = sorted(path for path in self.files if path.startswith("drops/"))
        self.assertEqual(drops, [f"drops/metricas_{month}.csv" for month in EXPECTED_MONTHS])
        self.assertEqual(
            Counter(self.manifest["files"][path]["schema"] for path in drops), {"v1": 31, "v2": 5}
        )
        case = self.manifest["scenarios"]["first_correction"]
        self.assertEqual((case["asesoria_id"], case["mes"]), (23, "2025-10"))
        row = self.by_reference[case["file"], case["line"]]
        original = self.by_reference[case["original_file"], case["original_line"]]
        self.assertIn("satisfaccion_media", row["raw"])
        self.assertIn("satisfaccion_cliente", original["raw"])
        self.assertEqual(row["raw"]["facturacion_otros_eur"], "")
        self.assertEqual(row["values"]["facturacion_otros_eur"], Decimal(0))
        for record in self.rows:
            if record["status"] != "rejected" and record["key"][1] >= "2025-11":
                self.assertGreater(record["values"]["facturacion_otros_eur"], 0)

    def test_corrections_win_and_have_unambiguous_lineage(self):
        accepted_by_key = defaultdict(list)
        for row in self.rows:
            if row["status"] == "accepted":
                accepted_by_key[row["key"]].append(row)
        self.assertEqual(Counter(len(rows) for rows in accepted_by_key.values()), {1: 17948, 2: 40})
        corrected = {key for key, rows in accepted_by_key.items() if len(rows) == 2}
        self.assertEqual(
            corrected,
            {(entry["asesoria_id"], entry["mes"]) for entry in self.manifest["corrections"]},
        )
        for entry in self.manifest["corrections"]:
            key = entry["asesoria_id"], entry["mes"]
            previous = self.by_reference[entry["original_file"], entry["original_line"]]
            selected = self.by_reference[entry["file"], entry["line"]]
            self.assertGreater(selected["file"], previous["file"])
            self.assertEqual(self.winners[key], selected)
            changed = {
                column
                for column in selected["values"]
                if selected["values"][column] != previous["values"][column]
            }
            self.assertEqual(changed, set(entry["changed_fields"]))
            for column, change in entry["changed_fields"].items():
                self.assertEqual(previous["values"][column], Decimal(change["before"]))
                self.assertEqual(selected["values"][column], Decimal(change["after"]))
                self.assertGreater(selected["values"][column], previous["values"][column])
        investigation = [row for row in self.rows if row["key"] == (42, "2024-01")]
        self.assertEqual(len(investigation), 1)
        self.assertEqual(investigation[0]["status"], "accepted")
        self.assertEqual(investigation[0]["issues"], [])
        reference = self.manifest["scenarios"]["investigation"]
        self.assertEqual(self.by_reference[reference["file"], reference["line"]], investigation[0])

    def test_rejections_never_replace_valid_rows(self):
        fallbacks = set()
        for row in self.rows:
            if row["status"] != "rejected":
                continue
            self.assertEqual(len(row["issues"]), 1)
            if row["key"] in self.winners:
                winner = self.winners[row["key"]]
                self.assertLess(winner["file"], row["file"])
                self.assertEqual(winner["status"], "accepted")
                fallbacks.add(row["key"])
        self.assertEqual(len(fallbacks), 14)
        self.assertEqual(self.manifest["expected_quality"]["fallback_rows"], len(fallbacks))
        for case in self.manifest["issue_scenarios"]:
            if "retained_key" in case:
                self.assertIn(
                    (case["retained_key"]["asesoria_id"], case["retained_key"]["mes"]), fallbacks
                )

    def test_warning_values_are_preserved_in_publication(self):
        nulls = Counter()
        backlog = 0
        for row in self.winners.values():
            for column in NULLABLE:
                nulls[column] += row["values"][column] is None
            backlog += row["values"]["consultas_resueltas"] > row["values"]["consultas_recibidas"]
        self.assertEqual(nulls, {column: 8 for column in NULLABLE})
        self.assertEqual(self.manifest["expected_quality"]["optional_nulls_in_publication"], nulls)
        self.assertEqual(backlog, 18)
        for row in self.rows:
            if any(severity == "warning" for _, severity, _ in row["issues"]):
                self.assertEqual(row["status"], "accepted")
                self.assertEqual(self.winners[row["key"]], row)

    def test_monthly_totals_reconcile_independently(self):
        self.assertEqual(self.manifest["monthly_totals"], self.totals)
        self.assertEqual(sum(item["reporting_firms"] for item in self.totals.values()), 17988)
        self.assertEqual(
            Counter(item["reporting_firms"] for item in self.totals.values()), {499: 12, 500: 24}
        )
        for item in self.totals.values():
            self.assertRegex(item["revenue_eur"], r"^\d+\.\d{2}$")
            self.assertIsInstance(item["clientes_activos"], int)
            self.assertGreater(Decimal(item["revenue_eur"]), 0)

    def test_manifest_scenarios_checksums_and_per_file_counts(self):
        self.assertEqual(self.manifest["manifest_version"], 1)
        self.assertTrue(self.manifest["synthetic"])
        self.assertEqual(self.manifest["seed"], generator.DEFAULT_SEED)
        self.assertEqual(set(self.manifest["files"]), set(self.files) - {"manifest.json"})
        self.assertEqual(len(self.manifest["files"]), 37)
        self.assertEqual(
            self.manifest["counts"],
            {
                "firms": 500,
                "months": 36,
                "monthly_files": 36,
                "csv_files": 37,
                "expected_firm_months": 18000,
                "base_rows": 17988,
                "correction_rows": 40,
            },
        )
        for path, metadata in self.manifest["files"].items():
            content = self.files[path]
            self.assertEqual(metadata["sha256"], hashlib.sha256(content).hexdigest())
            self.assertEqual(metadata["bytes"], len(content))
            self.assertNotIn(b"\r", content)
            if path.startswith("drops/"):
                rows = [row for row in self.rows if row["file"] == path]
                self.assertEqual(metadata["rows"], len(rows))
                self.assertEqual(
                    metadata["expected_status_counts"],
                    {
                        status: sum(row["status"] == status for row in rows)
                        for status in ("accepted", "duplicate", "rejected")
                    },
                )
        self.assertEqual(len(self.manifest["issue_scenarios"]), 101)
        for entry in self.manifest["issue_scenarios"]:
            row = self.by_reference[entry["file"], entry["line"]]
            self.assertEqual(row["key"], (entry["asesoria_id"], entry["mes"]))
            self.assertEqual(row["status"], entry["expected_status"])
            self.assertEqual(
                row["issues"],
                [(issue["code"], issue["severity"], issue["field"]) for issue in entry["issues"]],
            )

    def test_cli_reproducibility_and_read_only_check(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-test-") as temporary:
            temporary = Path(temporary)
            data = temporary / "data"

            def run(*args):
                return subprocess.run(
                    [sys.executable, "-B", str(SCRIPT), "--output", str(data), *args],
                    cwd=temporary,
                    capture_output=True,
                    text=True,
                    check=False,
                )

            def snapshot():
                return {
                    path.relative_to(data).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
                    for path in data.rglob("*")
                    if path.is_file()
                }

            self.assertEqual(run("--check").returncode, 1)
            self.assertFalse(data.exists(), "--check must not create a missing directory")
            generated = run()
            self.assertEqual(generated.returncode, 0, generated.stderr)
            self.assertEqual(
                {path: content for path, (content, _) in snapshot().items()}, self.files
            )
            before = snapshot()
            self.assertEqual(run("--check").returncode, 0)
            self.assertEqual(snapshot(), before)
            altered = data / "drops" / "metricas_2024-08.csv"
            altered.write_bytes(altered.read_bytes() + b"\n")
            (data / "asesorias.csv").unlink()
            (data / "drops" / "metricas_2026-04.csv").write_bytes(b"stale\n")
            before = snapshot()
            checked = run("--check")
            self.assertEqual(checked.returncode, 1)
            self.assertIn("different: drops/metricas_2024-08.csv", checked.stderr)
            self.assertIn("missing: asesorias.csv", checked.stderr)
            self.assertIn("unexpected CSV: drops/metricas_2026-04.csv", checked.stderr)
            self.assertEqual(snapshot(), before)
            self.assertEqual(
                run().returncode, 1, "Generation must refuse stale CSVs before writing"
            )
            self.assertEqual(snapshot(), before)


if __name__ == "__main__":
    unittest.main()
