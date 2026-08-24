from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..models import DailyRoll, GroupRoll
from ..schemas import (
    DailyRollGetOrCreateRequest,
    DailyRollItem,
    DailyRollListResponse,
    DailyRollLookupResponse,
    DailyRollOutcomeSnapshot,
    DailyRollSnapshotRequest,
    DailyRollSnapshotUpdateResponse,
)
from ..services.progress import apply_created_roll_progress, build_lookup_response
from ..services.reservations import activate_target_reservations
from ..services.roast_refills import mark_group_active_users

router = APIRouter(prefix="/v1/daily-rolls", tags=["daily-rolls"], dependencies=[Depends(verify_token)])


def _ensure_group_roll(session: Session, group_id: str, user_id: str, pig_id: str, date_str: dt.date) -> None:
    if not group_id:
        return
    existing = session.execute(
        select(GroupRoll).where(
            GroupRoll.group_id == group_id,
            GroupRoll.user_id == user_id,
            GroupRoll.date_str == date_str,
        )
    ).scalar_one_or_none()
    if existing:
        if existing.pig_id != pig_id:
            existing.pig_id = pig_id
    else:
        session.add(GroupRoll(group_id=group_id, user_id=user_id, pig_id=pig_id, date_str=date_str))
    mark_group_active_users(
        session,
        date_str=date_str,
        group_id=group_id,
        user_ids=[user_id],
    )


def _reconcile_reservations_after_commit(
    session: Session,
    *,
    date_str: dt.date,
    user_id: str,
    pig_id: str,
) -> None:
    """提交 DailyRoll 后再次幂等对账，封住预约与抽猪并发提交的交错窗口。"""

    if activate_target_reservations(
        session,
        date_str=date_str,
        target_id=user_id,
        target_pig_id=pig_id,
    ):
        session.commit()


@router.post("/get-or-create", response_model=DailyRollLookupResponse)
def get_or_create_daily_roll(req: DailyRollGetOrCreateRequest, session: Session = Depends(get_session)):
    existing = session.execute(
        select(DailyRoll).where(DailyRoll.user_id == req.user_id, DailyRoll.date_str == req.date_str)
    ).scalar_one_or_none()
    if existing:
        _ensure_group_roll(session, req.group_id, req.user_id, existing.pig_id, req.date_str)
        session.commit()
        _reconcile_reservations_after_commit(
            session,
            date_str=req.date_str,
            user_id=req.user_id,
            pig_id=existing.pig_id,
        )
        return build_lookup_response(session, daily_roll=existing, created=False)

    try:
        created = DailyRoll(user_id=req.user_id, pig_id=req.proposed_pig_id, date_str=req.date_str)
        session.add(created)
        progress = apply_created_roll_progress(
            session, req.user_id, req.proposed_pig_id
        )
        created.is_new_pig = progress.is_new_pig
        created.previous_copies = progress.previous_copies
        created.copies_after_roll = progress.copies_after_roll
        created.collection_size_after_roll = progress.collection_size_after_roll
        created.previous_duplicate_streak = progress.previous_duplicate_streak
        created.duplicate_streak_after_roll = progress.duplicate_streak_after_roll
        _ensure_group_roll(session, req.group_id, req.user_id, req.proposed_pig_id, req.date_str)
        activate_target_reservations(
            session,
            date_str=req.date_str,
            target_id=req.user_id,
            target_pig_id=req.proposed_pig_id,
        )
        session.commit()
        _reconcile_reservations_after_commit(
            session,
            date_str=req.date_str,
            user_id=req.user_id,
            pig_id=req.proposed_pig_id,
        )
        return build_lookup_response(session, daily_roll=created, created=True)
    except IntegrityError:
        session.rollback()
        existing = session.execute(
            select(DailyRoll).where(DailyRoll.user_id == req.user_id, DailyRoll.date_str == req.date_str)
        ).scalar_one()
        _ensure_group_roll(session, req.group_id, req.user_id, existing.pig_id, req.date_str)
        session.commit()
        _reconcile_reservations_after_commit(
            session,
            date_str=req.date_str,
            user_id=req.user_id,
            pig_id=existing.pig_id,
        )
        return build_lookup_response(session, daily_roll=existing, created=False)


@router.get("/by-date", response_model=DailyRollLookupResponse)
def get_daily_roll_by_date(user_id: str, date_str: dt.date, session: Session = Depends(get_session)):
    existing = session.execute(
        select(DailyRoll).where(DailyRoll.user_id == user_id, DailyRoll.date_str == date_str)
    ).scalar_one_or_none()
    if not existing:
        return DailyRollLookupResponse(pig_id=None, created=False)
    return build_lookup_response(session, daily_roll=existing, created=False)


# ================================ 抽取外观快照补全 ================================ #


def _snapshot_payload(req: DailyRollSnapshotRequest) -> dict:
    """生成稳定 JSON 结构，供首次写入与幂等重试逐字段比较。"""

    return {
        "resolved_variant_level": req.resolved_variant_level,
        "resolved_image_name": req.resolved_image_name,
        "unlocked_variant_levels": list(req.unlocked_variant_levels),
        "unlocked_variant_fields": list(req.unlocked_variant_fields),
    }


@router.put("/snapshot", response_model=DailyRollSnapshotUpdateResponse)
def complete_daily_roll_snapshot(req: DailyRollSnapshotRequest, session: Session = Depends(get_session)):
    """幂等保存抽取客户端当时实际解析出的资源与 EX 差分结果。"""

    existing = session.execute(
        select(DailyRoll).where(
            DailyRoll.user_id == req.user_id,
            DailyRoll.date_str == req.date_str,
        ).with_for_update()
    ).scalar_one_or_none()
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="每日抽取记录不存在")
    if existing.pig_id != req.pig_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="小猪 ID 与每日抽取记录不一致")
    if any(
        value is None
        for value in (
            existing.is_new_pig,
            existing.previous_copies,
            existing.copies_after_roll,
            existing.collection_size_after_roll,
            existing.previous_duplicate_streak,
            existing.duplicate_streak_after_roll,
        )
    ):
        # 旧记录没有抽取时结果，禁止用今天的资源反向补写历史上下文。
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="旧抽取记录不支持补全历史快照")

    payload = _snapshot_payload(req)
    has_existing_snapshot = existing.resource_version is not None or existing.appearance_snapshot is not None
    if has_existing_snapshot:
        stored_payload = existing.appearance_snapshot if isinstance(existing.appearance_snapshot, dict) else {}
        if str(existing.resource_version or "") != req.resource_version or stored_payload != payload:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="每日抽取快照已由首次客户端写入")
    else:
        existing.resource_version = req.resource_version
        existing.appearance_snapshot = payload
        session.commit()

    return DailyRollSnapshotUpdateResponse(
        outcome_snapshot=DailyRollOutcomeSnapshot(
            snapshot_available=True,
            collection_size_after_roll=int(existing.collection_size_after_roll or 0),
            resource_version=req.resource_version,
            **payload,
        )
    )


@router.get("/all", response_model=DailyRollListResponse)
def get_daily_rolls(date_str: dt.date, session: Session = Depends(get_session)):
    rows = session.execute(select(DailyRoll).where(DailyRoll.date_str == date_str)).scalars().all()
    return DailyRollListResponse(items=[DailyRollItem(user_id=row.user_id, pig_id=row.pig_id) for row in rows])
