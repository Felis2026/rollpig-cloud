from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..models import GroupRoll, RoastEvent
from ..schemas import ActiveGroupsResponse

router = APIRouter(prefix="/v1/groups", tags=["groups"], dependencies=[Depends(verify_token)])


@router.get("/active", response_model=ActiveGroupsResponse)
def get_active_groups(date_str: dt.date, session: Session = Depends(get_session)):
    event_rows = session.execute(select(RoastEvent.group_id).where(RoastEvent.date_str == date_str)).all()
    roll_rows = session.execute(select(GroupRoll.group_id).where(GroupRoll.date_str == date_str)).all()
    group_ids = sorted({str(row[0]) for row in event_rows + roll_rows if row[0]})
    return ActiveGroupsResponse(group_ids=group_ids)
