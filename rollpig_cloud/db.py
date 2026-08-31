from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


def _engine_connect_args(database_url: str) -> dict[str, str]:
    """为 MySQL 会话固定 UTC，保证服务端时间戳与 UTC naive 截止点口径一致。"""

    if make_url(database_url).get_backend_name() == "mysql":
        # 使用数值偏移不依赖 MySQL 时区表，PyMySQL 会在每条新连接建立时执行。
        return {"init_command": "SET time_zone = '+00:00'"}
    return {}


engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    future=True,
    connect_args=_engine_connect_args(settings.database_url),
)
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

    Base.metadata.create_all(bind=engine)
    # 回填仅扫描业务日期今天与昨天，且只会把 active_at 向更早的真实来源时间修正。
    # 每次启动执行可修复 0.5.1 已写入部署时间的旧行，同时保持重复启动幂等。
    ensure_runtime_migrations(
        engine,
        backfill_group_activity=True,
    )
