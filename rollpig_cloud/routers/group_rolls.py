from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..models import GroupRoll
from ..schemas import GroupRollItem, GroupRollListResponse, GroupRollMarkSeenRequest

router = APIRouter(prefix="/v1/group-rolls", tags=["group-rolls"], dependencies=[Depends(verify_token)])


@router.post("/mark-seen")
def mark_seen(req: GroupRollMarkSeenRequest, session: Session = Depends(get_session)):
    existing = session.execute(
        select(GroupRoll).where(
            GroupRoll.group_id == req.group_id,
            GroupRoll.user_id == req.user_id,
            GroupRoll.date_str == req.date_str,
        )
    ).scalar_one_or_none()
    if existing:
        existing.pig_id = req.pig_id
    else:
        session.add(GroupRoll(group_id=req.group_id, user_id=req.user_id, pig_id=req.pig_id, date_str=req.date_str))
    session.commit()
    return {"ok": True}


@router.get("", response_model=GroupRollListResponse)
def get_group_rolls(group_id: str, date_str: dt.date, session: Session = Depends(get_session)):
    rows = session.execute(
        select(GroupRoll).where(GroupRoll.group_id == group_id, GroupRoll.date_str == date_str)
    ).scalars().all()
    return GroupRollListResponse(items=[GroupRollItem(user_id=row.user_id, pig_id=row.pig_id) for row in rows])
