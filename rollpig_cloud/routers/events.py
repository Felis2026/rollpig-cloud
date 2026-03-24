from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..models import RoastEvent
from ..schemas import EventCreateRequest, EventItem, EventListResponse

router = APIRouter(prefix="/v1/events", tags=["events"], dependencies=[Depends(verify_token)])


@router.post("")
def create_event(req: EventCreateRequest, session: Session = Depends(get_session)):
    target_date = req.date_str or dt.date.today()
    session.add(
        RoastEvent(
            date_str=target_date,
            group_id=req.group_id,
            event_type=req.event_type,
            attacker_id=req.attacker_id,
            target_id=req.target_id,
            attacker_name=req.attacker_name,
            target_name=req.target_name,
            food_name=req.food,
        )
    )
    session.commit()
    return {"ok": True}


@router.get("", response_model=EventListResponse)
def list_events(date_str: dt.date, group_id: str | None = None, session: Session = Depends(get_session)):
    stmt = select(RoastEvent).where(RoastEvent.date_str == date_str)
    if group_id:
        stmt = stmt.where(RoastEvent.group_id == group_id)
    rows = session.execute(stmt).scalars().all()
    return EventListResponse(
        items=[
            EventItem(
                type=row.event_type,
                attacker=row.attacker_id,
                target=row.target_id,
                attacker_name=row.attacker_name,
                target_name=row.target_name,
                food=row.food_name,
                group_id=row.group_id,
            )
            for row in rows
        ]
    )
