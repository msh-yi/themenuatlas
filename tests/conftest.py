import os
import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_text():
    return lambda name: (FIXTURES / name).read_text()


@pytest.fixture(scope="session")
def pg_url():
    """A throwaway local Postgres with the Alembic schema applied."""
    # pgserver keeps a lock under XDG_RUNTIME_DIR, which may not exist or be
    # writable (WSL). It reads the variable at import time, so set it first.
    tmp = Path(tempfile.mkdtemp(prefix="menudb-pg-"))
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not os.access(runtime, os.W_OK):
        os.environ["XDG_RUNTIME_DIR"] = str(tmp)

    pgserver = pytest.importorskip("pgserver")
    from alembic import command
    from alembic.config import Config
    srv = pgserver.get_server(tmp / "data", cleanup_mode="delete")
    url = srv.get_uri().replace("postgresql://", "postgresql+psycopg://")

    os.environ["MENUDB_MIGRATION_URL"] = url
    command.upgrade(Config(str(Path(__file__).parents[1] / "alembic.ini")), "head")
    yield url
    os.environ.pop("MENUDB_MIGRATION_URL", None)
