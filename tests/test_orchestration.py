from dagster import DefaultSensorStatus, Definitions

from pulse.definitions import csv_delivery_sensor, daily_ingestion, defs


def test_definitions_and_schedule_load():
    Definitions.validate_loadable(defs)
    assert daily_ingestion.cron_schedule == "0 6 * * *"
    assert daily_ingestion.execution_timezone == "Europe/Madrid"
    assert csv_delivery_sensor.minimum_interval_seconds == 30
    assert csv_delivery_sensor.default_status == DefaultSensorStatus.RUNNING
