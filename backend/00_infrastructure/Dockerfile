FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DAGSTER_HOME=/opt/dagster PULSE_FRONTEND_DIR=/app/frontend
WORKDIR /app/backend
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home --uid 10001 pulse && mkdir -p /opt/dagster && chown pulse /opt/dagster
COPY backend/pyproject.toml ./
COPY backend/01_pipeline ./01_pipeline
COPY backend/02_api ./02_api
COPY backend/04_agents ./04_agents
RUN pip install --no-deps --no-build-isolation .
COPY backend/00_infrastructure/dagster/dagster.yaml /opt/dagster/dagster.yaml
COPY backend/00_infrastructure/dagster/workspace.yaml ./workspace.yaml
COPY frontend/01_dashboard/public /app/frontend/01_dashboard/public
COPY frontend/02_lab/public /app/frontend/02_lab/public
USER pulse
CMD ["uvicorn", "pulse_api.app:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test
ENV RUFF_CACHE_DIR=/tmp/ruff-cache
USER root
COPY backend/requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY backend/tests ./tests
COPY backend/03_scripts ./03_scripts
COPY data ./data
USER pulse
CMD ["pytest", "-q", "-p", "no:cacheprovider"]
