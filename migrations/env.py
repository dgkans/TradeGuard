"""Alembic environment. Uses DATABASE_URL (validated by services.common.config)."""

from alembic import context
from sqlalchemy import create_engine

from services.common.config import load_settings


def sqlalchemy_url() -> str:
    """Explicit URL from the caller's config, else DATABASE_URL, using the psycopg 3 driver."""
    url = context.config.get_main_option("sqlalchemy.url") or (
        load_settings().database_url.get_secret_value()
    )
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def run_migrations_online() -> None:
    engine = create_engine(sqlalchemy_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
