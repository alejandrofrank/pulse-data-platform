from dagster import (
    AssetCheckResult,
    AssetSelection,
    Config,
    DefaultScheduleStatus,
    DefaultSensorStatus,
    Definitions,
    Failure,
    MaterializeResult,
    RetryPolicy,
    ScheduleDefinition,
    asset,
    asset_check,
    define_asset_job,
    sensor,
)

from pulse.db import connect
from pulse.file_sensor import evaluate_files
from pulse.ingest import ingest


class IngestionConfig(Config):
    file_names: list[str] | None = None
    expected_sha256: dict[str, str] | None = None


@asset(
    description="Archive new deliveries, validate rows and atomically publish firm-month metrics.",
    retry_policy=RetryPolicy(max_retries=2, delay=5),
)
def monthly_metrics(context, config: IngestionConfig):
    result = ingest(
        dagster_run_id=context.run.run_id,
        file_names=config.file_names,
        expected_sha256=config.expected_sha256,
    )
    context.log.info(str(result))
    if result["status"] != "success":
        raise Failure(
            "Some files were quarantined. Inspect /api/v1/quality/file-errors.",
            metadata=result,
            allow_retries=False,
        )
    return MaterializeResult(metadata=result)


@asset_check(asset=monthly_metrics, blocking=True)
def publication_integrity():
    with connect() as conn:
        bad = conn.execute("""SELECT count(*) AS n FROM mart.firm_month f
            JOIN raw.records r ON r.id=f.source_record_id WHERE r.status!='accepted'""").fetchone()
        count = conn.execute("SELECT count(*) AS n FROM mart.firm_month").fetchone()["n"]
    return AssetCheckResult(
        passed=bad["n"] == 0 and count > 0,
        metadata={"curated_rows": count, "invalid_sources": bad["n"]},
    )


ingestion_job = define_asset_job("ingest_all", selection=AssetSelection.all())
daily_ingestion = ScheduleDefinition(
    job=ingestion_job,
    cron_schedule="0 6 * * *",
    execution_timezone="Europe/Madrid",
    default_status=DefaultScheduleStatus.RUNNING,
)


@sensor(job=ingestion_job, minimum_interval_seconds=30, default_status=DefaultSensorStatus.RUNNING)
def csv_delivery_sensor(context):
    return evaluate_files(context)


defs = Definitions(
    assets=[monthly_metrics],
    asset_checks=[publication_integrity],
    jobs=[ingestion_job],
    schedules=[daily_ingestion],
    sensors=[csv_delivery_sensor],
)
