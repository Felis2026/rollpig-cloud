from __future__ import annotations

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)


def get_session():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db():
    from . import models  # noqa: F401
    from .migrations import ensure_runtime_migrations

    existing_tables = set(inspect(engine).get_table_names())
    Base.metadata.create_all(bind=engine)
    ensure_runtime_migrations(
        engine,
        backfill_group_activity="group_daily_active_users" not in existing_tables,
    )
