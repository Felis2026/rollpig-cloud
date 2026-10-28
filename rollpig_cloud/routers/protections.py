from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..config import ApiKeyIdentity, rollpig_today
from ..db import get_session, run_transaction_with_sqlite_lock_retry
from ..models import GroupProtection
from ..schemas import GrantProtectionRequest, GrantProtectionResponse, ProtectedResponse, ReplaceGroupProtectionsRequest
from ..services.key_usage import record_key_mutation_outcome
from ..services.protections import (
    inherit_global_protection, is_historical_daily_protection, protection_level, protection_levels,
    protection_source, protection_sources, sync_pending_protection,
)

router = APIRouter(prefix="/v1/protections", tags=["protections"], dependencies=[Depends(verify_token)])


def _run_protection_transaction(session: Session, transaction):
    """首次插入或 MySQL 行锁竞争失败时，回滚并重做整笔保护写入。"""
    for attempt in range(3):
        try:
            return run_transaction_with_sqlite_lock_retry(session, transaction)
        except (IntegrityError, OperationalError) as error:
            session.rollback()
            mysql_conflict = session.get_bind().dialect.name == "mysql" and getattr(error.orig, "args", (None,))[0] in {1205, 1213}
            if attempt == 2 or not (isinstance(error, IntegrityError) or mysql_conflict):
                raise


@router.post("/replace-group")
def replace_group(
    req: ReplaceGroupProtectionsRequest,
    session: Session = Depends(get_session),
    identity: ApiKeyIdentity = Depends(verify_token),
):
    """替换群保护名单并保留已有保护等级。

    SQLite 在读取名单前取得写锁，写锁冲突时回滚并重跑完整事务。
    """

    def transaction():
        """在同一事务中更新保护名单、起始等级和调用统计。"""

        if session.get_bind().dialect.name == "sqlite":
            # 空名单也先取得数据库写锁，防止两个请求同时读空并插入相同保护记录。
            session.execute(update(GroupProtection).where(
                GroupProtection.group_id == req.group_id,
                GroupProtection.protect_date == req.protect_date,
            ).values(base_level=GroupProtection.base_level))
        # 日报重试可能重复写入同一天名单，保留已有保护者累积的成功次数。
        current = {row.user_id: row for row in session.scalars(
            select(GroupProtection).where(GroupProtection.group_id == req.group_id,
                                          GroupProtection.protect_date == req.protect_date)
            .with_for_update().execution_options(populate_existing=True)
        )}
        user_ids = sorted({str(user_id) for user_id in req.user_ids if user_id})
        previous = {row.user_id: row for row in session.scalars(select(GroupProtection).where(
            GroupProtection.group_id == req.group_id,
            GroupProtection.protect_date == req.protect_date - dt.timedelta(days=1),
        ))}
        removed = set(current) - set(user_ids)
        deleted = 0
        for user_id in removed:
            row = current[user_id]
            sources = protection_sources(row)
            sources.pop("daily", None)
            historical_daily = is_historical_daily_protection(row)
            if historical_daily:
                sources.pop("legacy", None)
            legacy = session.scalar(select(GroupProtection).where(
                GroupProtection.protect_date == req.protect_date,
                GroupProtection.group_id == "__all__",
                GroupProtection.user_id == user_id,
            ).with_for_update().execution_options(populate_existing=True)) if req.group_id != "__all__" else None
            baselines = dict(row.source_baselines or {})
            if legacy is None:
                sources.pop("legacy", None)
            else:
                # 全局来源独立于日报保留，本群破防升级也随继承来源保存。
                inherited_level = protection_levels(row).get("legacy", 0) if not historical_daily else 0
                global_level = protection_level(legacy)
                if global_level > inherited_level:
                    sources["legacy"] = global_level
                    baselines["legacy"] = row.roast_success_count
            if sources:
                row.source_levels = sources
                row.source_baselines = {source: value for source, value in baselines.items() if source in sources}
                row.base_level = max(sources.values())
                row.consecutive_days = 0
                sync_pending_protection(session, row)
            else:
                sync_pending_protection(session, row, removed=True)
                session.delete(row)
                deleted += 1
        for user_id in user_ids:
            row = current.get(user_id)
            if row is None and req.group_id != "__all__":
                legacy = session.scalar(select(GroupProtection).where(
                    GroupProtection.protect_date == req.protect_date,
                    GroupProtection.group_id == "__all__",
                    GroupProtection.user_id == user_id,
                ).with_for_update().execution_options(populate_existing=True))
                if legacy is not None:
                    row = inherit_global_protection(legacy, req.group_id)
                    session.add(row)
            sources = protection_sources(row) if row is not None else {}
            if "daily" in sources:
                continue
            yesterday = previous.get(user_id)
            previous_days = yesterday.consecutive_days if yesterday is not None else 0
            days = previous_days + 1 if previous_days else 1
            baselines = dict(row.source_baselines or {}) if row is not None else {}
            if row is not None and is_historical_daily_protection(row):
                # 只有历史日报记录可转为 daily，继承的全局来源继续独立保留。
                daily_level = sources.pop("legacy")
                days = row.consecutive_days
                baselines["daily"] = baselines.pop("legacy", 0)
            else:
                daily_level = min(5, req.initial_level + days - 1)
                baselines["daily"] = row.roast_success_count if row is not None else 0
            sources["daily"] = daily_level
            if row is None:
                row = GroupProtection(protect_date=req.protect_date, group_id=req.group_id, user_id=user_id,
                                      roast_success_count=0)
                session.add(row)
            row.source_levels = sources
            row.source_baselines = baselines
            row.base_level = max(sources.values())
            row.consecutive_days = days
            sync_pending_protection(session, row)
        record_key_mutation_outcome(
            session,
            identity,
            operation="POST /v1/protections/replace-group",
            created_records=len(set(user_ids) - set(current)),
            deleted_records=deleted,
        )
        session.commit()
        return {"ok": True}

    return _run_protection_transaction(session, transaction)


@router.post("/grant", response_model=GrantProtectionResponse)
def grant_protection(
    req: GrantProtectionRequest,
    session: Session = Depends(get_session),
    identity: ApiKeyIdentity = Depends(verify_token),
):
    """为单个群友发放当日保护；重复请求与低等级请求保持现有权益。"""
    def transaction():
        """同一事务内更新保护来源和等待中的预约，不消费烧烤额度。"""
        if req.protect_date != rollpig_today():
            raise HTTPException(status_code=422, detail="protect_date must be today")
        if session.get_bind().dialect.name == "sqlite":
            session.execute(update(GroupProtection).where(
                GroupProtection.group_id == req.group_id,
                GroupProtection.protect_date == req.protect_date,
            ).values(base_level=GroupProtection.base_level))
        row = session.scalar(select(GroupProtection).where(
            GroupProtection.protect_date == req.protect_date,
            GroupProtection.group_id == req.group_id,
            GroupProtection.user_id == req.user_id,
        ).with_for_update().execution_options(populate_existing=True))
        created = row is None
        old_sources = row.source_levels if row is not None else None
        legacy = None
        if row is None:
            legacy = session.scalar(select(GroupProtection).where(
                GroupProtection.protect_date == req.protect_date,
                GroupProtection.group_id == "__all__",
                GroupProtection.user_id == req.user_id,
            ).with_for_update().execution_options(populate_existing=True))
            row = inherit_global_protection(legacy, req.group_id) if legacy is not None else GroupProtection(
                protect_date=req.protect_date, group_id=req.group_id, user_id=req.user_id,
                base_level=req.level, consecutive_days=0, roast_success_count=0, source_levels={"manual": req.level},
            )
            session.add(row)
        if not created or legacy is not None:
            level = protection_level(row)
            changed = req.level > level
            sources = protection_sources(row)
            baselines = dict(row.source_baselines or {})
            if req.level > protection_levels(row).get("manual", 0):
                sources["manual"] = req.level
                baselines["manual"] = row.roast_success_count
            row.source_levels = sources
            row.source_baselines = baselines
            row.base_level = max(sources.values())
        else:
            changed = True
        session.flush()
        sync_pending_protection(session, row)
        result = GrantProtectionResponse(level=protection_level(row), changed=changed)
        record_key_mutation_outcome(session, identity, operation="POST /v1/protections/grant",
                                   created_records=int(created),
                                   updated_records=int(not created and old_sources != row.source_levels),
                                   idempotent_hits=int(not created and old_sources == row.source_levels))
        session.commit()
        return result

    return _run_protection_transaction(session, transaction)


@router.get("/check", response_model=ProtectedResponse)
def check_protected(protect_date: dt.date, group_id: str, user_id: str, session: Session = Depends(get_session)):
    row = session.execute(
        select(GroupProtection).where(
            GroupProtection.protect_date == protect_date,
            GroupProtection.user_id == user_id,
            or_(GroupProtection.group_id == group_id, GroupProtection.group_id == "__all__"),
        ).order_by((GroupProtection.group_id == group_id).desc()).limit(1)
    ).scalar_one_or_none()
    return ProtectedResponse(protected=bool(row), base_level=row.base_level if row else 0,
                             level=protection_level(row) if row else 0,
                             consecutive_days=row.consecutive_days if row else 0,
                             source=protection_source(row) if row else "legacy",
                             daily_base_level=(protection_sources(row).get("daily", row.base_level if row.source_levels is None else 0) if row else 0))
