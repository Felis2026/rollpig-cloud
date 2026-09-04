from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine, func
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from .config import ROLLPIG_TIMEZONE, settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, class_=Session)


def database_cutoff_value(
    session: Session,
    value: dt.datetime,
) -> dt.datetime | ColumnElement[dt.datetime]:
    """把固定截止点转换为当前后端既有 DateTime 存储口径。"""

    if value.tzinfo is None:
        # 保持旧接口约定：无时区值由调用方按数据库本地时间负责解释。
        return value
    utc_value = value.astimezone(dt.timezone.utc)
    if session.get_bind().dialect.name == "mysql":
        # 历史 MySQL DATETIME 使用部署原有会话时区。FROM_UNIXTIME 会在数据库端按
        # 同一会话时区还原墙上时间，不修改旧数据，也不要求部署者迁移时区。
        return func.from_unixtime(utc_value.timestamp())
    # SQLite CURRENT_TIMESTAMP 固定使用 UTC，继续沿用原来的 UTC-naive 比较口径。
    return utc_value.replace(tzinfo=None)


def database_datetime_for_response(session: Session, value: dt.datetime) -> dt.datetime:
    """按当前数据库的存储口径为响应时间补充明确时区。"""

    if value.tzinfo is not None:
        return value
    if session.get_bind().dialect.name == "mysql":
        # 生产 MySQL DATETIME 由数据库会话按 RollPig 业务时区写入和读取。
        return value.replace(tzinfo=ROLLPIG_TIMEZONE)
    # SQLite CURRENT_TIMESTAMP 使用 UTC，测试与本地部署继续按 UTC 解释。
    return value.replace(tzinfo=dt.timezone.utc)


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
