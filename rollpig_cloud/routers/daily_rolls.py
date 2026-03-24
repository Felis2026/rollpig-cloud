from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..models import Collection, DailyRoll, GroupRoll
from ..schemas import DailyRollGetOrCreateRequest, DailyRollItem, DailyRollListResponse, DailyRollLookupResponse

router = APIRouter(prefix="/v1/daily-rolls", tags=["daily-rolls"], dependencies=[Depends(verify_token)])


def _ensure_collection(session: Session, user_id: str, pig_id: str) -> None:
    exists = session.execute(
        select(Collection).where(Collection.user_id == user_id, Collection.pig_id == pig_id)
    ).scalar_one_or_none()
    if not exists:
        session.add(Collection(user_id=user_id, pig_id=pig_id))


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


@router.post("/get-or-create", response_model=DailyRollLookupResponse)
def get_or_create_daily_roll(req: DailyRollGetOrCreateRequest, session: Session = Depends(get_session)):
    existing = session.execute(
        select(DailyRoll).where(DailyRoll.user_id == req.user_id, DailyRoll.date_str == req.date_str)
    ).scalar_one_or_none()
    if existing:
        _ensure_group_roll(session, req.group_id, req.user_id, existing.pig_id, req.date_str)
        session.commit()
        return DailyRollLookupResponse(pig_id=existing.pig_id, created=False)

    try:
        created = DailyRoll(user_id=req.user_id, pig_id=req.proposed_pig_id, date_str=req.date_str)
        session.add(created)
        _ensure_collection(session, req.user_id, req.proposed_pig_id)
        _ensure_group_roll(session, req.group_id, req.user_id, req.proposed_pig_id, req.date_str)
        session.commit()
        return DailyRollLookupResponse(pig_id=req.proposed_pig_id, created=True)
    except IntegrityError:
        session.rollback()
        existing = session.execute(
            select(DailyRoll).where(DailyRoll.user_id == req.user_id, DailyRoll.date_str == req.date_str)
        ).scalar_one()
        _ensure_group_roll(session, req.group_id, req.user_id, existing.pig_id, req.date_str)
        session.commit()
        return DailyRollLookupResponse(pig_id=existing.pig_id, created=False)


@router.get("/by-date", response_model=DailyRollLookupResponse)
def get_daily_roll_by_date(user_id: str, date_str: dt.date, session: Session = Depends(get_session)):
    existing = session.execute(
        select(DailyRoll).where(DailyRoll.user_id == user_id, DailyRoll.date_str == date_str)
    ).scalar_one_or_none()
    return DailyRollLookupResponse(pig_id=existing.pig_id if existing else None, created=False)


@router.get("/all", response_model=DailyRollListResponse)
def get_daily_rolls(date_str: dt.date, session: Session = Depends(get_session)):
    rows = session.execute(select(DailyRoll).where(DailyRoll.date_str == date_str)).scalars().all()
    return DailyRollListResponse(items=[DailyRollItem(user_id=row.user_id, pig_id=row.pig_id) for row in rows])
