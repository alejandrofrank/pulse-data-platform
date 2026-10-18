FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DAGSTER_HOME=/opt/dagster
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home --uid 10001 pulse && mkdir -p /opt/dagster && chown pulse /opt/dagster
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-deps --no-build-isolation .
COPY orchestration/dagster.yaml /opt/dagster/dagster.yaml
COPY orchestration/workspace.yaml /app/workspace.yaml
USER pulse
CMD ["uvicorn", "pulse.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test
ENV RUFF_CACHE_DIR=/tmp/ruff-cache
USER root
COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests
COPY scripts ./scripts
COPY data ./data
USER pulse
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

