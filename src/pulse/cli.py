import argparse
import os

from pulse.db import migrate


def main():
    parser = argparse.ArgumentParser(description="Pulse data platform")
    parser.add_argument("command", choices=["bootstrap", "migrate", "ingest"])
    parser.add_argument("--data-dir", help="Directory containing asesorias.csv and drops/")
    args = parser.parse_args()
    if args.data_dir:
        os.environ["PULSE_DATA_DIR"] = args.data_dir
    if args.command in ("bootstrap", "migrate"):
        migrate()
    if args.command in ("bootstrap", "ingest"):
        from dagster import DagsterInstance

        from pulse.definitions import defs

        with DagsterInstance.get() as instance:
            result = defs.get_job_def("ingest_all").execute_in_process(instance=instance)
        if not result.success:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
