from __future__ import annotations

import datetime as dt

from sqlalchemy import BIGINT, Date, DateTime, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class DailyRoll(Base):
    __tablename__ = "daily_rolls"
    __table_args__ = (
        UniqueConstraint("date_str", "user_id", name="uq_daily_roll_date_user"),
        Index("ix_daily_rolls_date", "date_str"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    date_str: Mapped[dt.date] = mapped_column(Date, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pig_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class GroupRoll(Base):
    __tablename__ = "group_rolls"
    __table_args__ = (
        UniqueConstraint("date_str", "group_id", "user_id", name="uq_group_roll_date_group_user"),
        Index("ix_group_rolls_date_group", "date_str", "group_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    date_str: Mapped[dt.date] = mapped_column(Date, nullable=False)
    group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pig_id: Mapped[str] = mapped_column(String(128), nullable=False)
    seen_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class Collection(Base):
    __tablename__ = "collections"
    __table_args__ = (UniqueConstraint("user_id", "pig_id", name="uq_collection_user_pig"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pig_id: Mapped[str] = mapped_column(String(128), nullable=False)
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


# ================================ P1A成长状态 ================================ #
# 先把重复抽猪成长数据独立成新表，不改旧主表结构，便于平滑上线和回滚。
# tenant_id 当前统一使用默认租户，避免后续内部扩展时改动既有表结构主键。
class UserPigProgress(Base):
    __tablename__ = "user_pig_progress"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "pig_id", name="uq_user_pig_progress_tenant_user_pig"),
        Index("ix_user_pig_progress_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pig_id: Mapped[str] = mapped_column(String(128), nullable=False)
    copies: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_obtained_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class UserDrawState(Base):
    __tablename__ = "user_draw_state"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_user_draw_state_tenant_user"),
        Index("ix_user_draw_state_tenant_user", "tenant_id", "user_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    duplicate_streak: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class UserUsage(Base):
    __tablename__ = "user_usage"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    last_roast_ts: Mapped[int | None] = mapped_column(BIGINT, nullable=True)
    last_force_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())


class RoastEvent(Base):
    __tablename__ = "roast_events"
    __table_args__ = (Index("ix_roast_events_date_group", "date_str", "group_id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    date_str: Mapped[dt.date] = mapped_column(Date, nullable=False)
    group_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    attacker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attacker_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    target_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    food_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())


class GroupProtection(Base):
    __tablename__ = "group_protections"
    __table_args__ = (
        UniqueConstraint("protect_date", "group_id", "user_id", name="uq_group_protection_date_group_user"),
        Index("ix_group_protection_date_group", "protect_date", "group_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    protect_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
