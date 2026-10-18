from dagster import (
    AssetCheckResult,
    AssetSelection,
    DefaultScheduleStatus,
    Definitions,
    Failure,
    MaterializeResult,
    ScheduleDefinition,
    asset,
    asset_check,
    define_asset_job,
)

from pulse.db import connect
from pulse.ingest import ingest


@asset(
    description="Archive new deliveries, validate rows and atomically publish firm-month metrics."
)
def monthly_metrics(context):
    result = ingest(dagster_run_id=context.run_id)
    context.log.info(str(result))
    if result["status"] != "success":
        raise Failure(
            "Some files were quarantined. Inspect /api/v1/quality/file-errors.", metadata=result
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
defs = Definitions(
    assets=[monthly_metrics],
    asset_checks=[publication_integrity],
    jobs=[ingestion_job],
    schedules=[daily_ingestion],
)
