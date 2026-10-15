from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from menudb.config import get_settings


def make_engine(url: str) -> Engine:
    # Neon's pooled endpoint is PgBouncer in transaction mode; turn off psycopg's
    # server-side prepared statements so they can't leak across pooled backends.
    return create_engine(url, pool_pre_ping=True, connect_args={"prepare_threshold": None})


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings().db_url)


def get_session() -> Session:
    return sessionmaker(get_engine(), expire_on_commit=False)()
