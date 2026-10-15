import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from menudb.config import get_settings
from menudb.db.models import Base

config = context.config
target_metadata = Base.metadata

# Migrations use the direct (unpooled) Neon URL; MENUDB_MIGRATION_URL overrides it for tests.
url = os.environ.get("MENUDB_MIGRATION_URL") or get_settings().migration_db_url
config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
