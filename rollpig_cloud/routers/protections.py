from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..config import ApiKeyIdentity
from ..db import get_session, run_transaction_with_sqlite_lock_retry
from ..models import GroupProtection
from ..schemas import ProtectedResponse, ReplaceGroupProtectionsRequest
from ..services.key_usage import record_key_mutation_outcome

router = APIRouter(prefix="/v1/protections", tags=["protections"], dependencies=[Depends(verify_token)])


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
        for user_id in removed:
            session.delete(current[user_id])
        for user_id in user_ids:
            if user_id in current:
                continue
            days = previous[user_id].consecutive_days + 1 if user_id in previous else 1
            session.add(GroupProtection(protect_date=req.protect_date, group_id=req.group_id, user_id=user_id,
                                        base_level=min(5, req.initial_level + days - 1), consecutive_days=days))
        record_key_mutation_outcome(
            session,
            identity,
            operation="POST /v1/protections/replace-group",
            created_records=len(set(user_ids) - set(current)),
            deleted_records=len(removed),
        )
        session.commit()
        return {"ok": True}

    return run_transaction_with_sqlite_lock_retry(session, transaction)


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
                             level=min(5, row.base_level + row.roast_success_count) if row else 0,
                             consecutive_days=row.consecutive_days if row else 0)
