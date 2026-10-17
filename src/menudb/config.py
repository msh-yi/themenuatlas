from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def _sqlalchemy_url(url: str) -> str:
    """Neon hands out libpq URLs; SQLAlchemy needs the psycopg (v3) driver named."""
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    return url


class Settings(BaseSettings):
    # .env.local is written by `neon env pull`; later files win.
    model_config = SettingsConfigDict(env_file=(".env", ".env.local"), extra="ignore")

    # Pooled (PgBouncer) for pipeline traffic, direct for Alembic migrations.
    database_url: str = ""
    database_url_unpooled: str = ""

    # Neon Object Storage (S3-compatible, path-style addressing required).
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_endpoint_url_s3: str = ""
    aws_region: str = "us-east-2"
    snapshots_bucket: str = "snapshots"

    neon_auth_base_url: str = ""
    neon_auth_jwks_url: str = ""
    contact_url: str = "https://github.com/msh-yi/themenuatlas"
    # Optional: a Chromium binary for render: js recipes when Playwright's own
    # download doesn't match (cloud sessions have one at /opt/pw-browsers/chromium).
    chromium_path: str = ""

    @property
    def db_url(self) -> str:
        return _sqlalchemy_url(self.database_url)

    @property
    def migration_db_url(self) -> str:
        return _sqlalchemy_url(self.database_url_unpooled or self.database_url)

    @property
    def user_agent(self) -> str:
        return f"themenuproject/0.1 (+{self.contact_url})"


@lru_cache
def get_settings() -> Settings:
    return Settings()
