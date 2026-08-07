from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..models import DailyRoll, GroupRoll
from ..schemas import DailyRollGetOrCreateRequest, DailyRollItem, DailyRollListResponse, DailyRollLookupResponse
from ..services.progress import apply_created_roll_progress, build_lookup_response
from ..services.reservations import activate_target_reservations

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
        return build_lookup_response(session, user_id=req.user_id, pig_id=existing.pig_id, created=False)

    try:
        created = DailyRoll(user_id=req.user_id, pig_id=req.proposed_pig_id, date_str=req.date_str)
        session.add(created)
        is_new_pig, previous_copies, previous_duplicate_streak = apply_created_roll_progress(
            session, req.user_id, req.proposed_pig_id
        )
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
        return build_lookup_response(
            session,
            user_id=req.user_id,
            pig_id=req.proposed_pig_id,
            created=True,
            is_new_pig=is_new_pig,
            previous_copies=previous_copies,
            previous_duplicate_streak=previous_duplicate_streak,
        )
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
        return build_lookup_response(session, user_id=req.user_id, pig_id=existing.pig_id, created=False)


@router.get("/by-date", response_model=DailyRollLookupResponse)
def get_daily_roll_by_date(user_id: str, date_str: dt.date, session: Session = Depends(get_session)):
    existing = session.execute(
        select(DailyRoll).where(DailyRoll.user_id == user_id, DailyRoll.date_str == date_str)
    ).scalar_one_or_none()
    if not existing:
        return DailyRollLookupResponse(pig_id=None, created=False)
    return build_lookup_response(session, user_id=user_id, pig_id=existing.pig_id, created=False)


@router.get("/all", response_model=DailyRollListResponse)
def get_daily_rolls(date_str: dt.date, session: Session = Depends(get_session)):
    rows = session.execute(select(DailyRoll).where(DailyRoll.date_str == date_str)).scalars().all()
    return DailyRollListResponse(items=[DailyRollItem(user_id=row.user_id, pig_id=row.pig_id) for row in rows])
