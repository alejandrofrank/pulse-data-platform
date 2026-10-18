import os
from urllib.parse import urlparse

import pytest

from pulse.db import connect, migrate


@pytest.fixture
def database(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to an isolated PostgreSQL database ending in _test")
    if not urlparse(url).path.endswith("_test"):
        raise RuntimeError("Refusing destructive tests outside a database named *_test")
    monkeypatch.setenv("DATABASE_URL", url)
    with connect() as conn:
        conn.execute("DROP SCHEMA IF EXISTS lab,mart,core,raw,ops CASCADE")
        conn.execute("DROP TABLE IF EXISTS public.schema_migrations")
    migrate()
    migrate()
    return url
