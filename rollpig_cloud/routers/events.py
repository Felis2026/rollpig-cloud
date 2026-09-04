from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..config import ApiKeyIdentity
from ..db import (
    database_cutoff_value,
    database_datetime_for_response,
    get_session,
)
from ..models import RoastEvent
from ..schemas import EventCreateRequest, EventItem, EventListResponse
from ..services.events import record_roast_event_with_status
from ..services.key_usage import record_key_mutation_outcome

router = APIRouter(prefix="/v1/events", tags=["events"], dependencies=[Depends(verify_token)])


@router.post("")
def create_event(
    req: EventCreateRequest,
    session: Session = Depends(get_session),
    identity: ApiKeyIdentity = Depends(verify_token),
):
    _recorded, created = record_roast_event_with_status(session, req)
    record_key_mutation_outcome(
        session,
        identity,
        operation="POST /v1/events",
        created_records=int(created),
        idempotent_hits=int(not created),
    )
    session.commit()
    return {"ok": True}


@router.get("", response_model=EventListResponse)
def list_events(
    date_str: dt.date,
    group_id: str | None = None,
    session: Session = Depends(get_session),
    user_id: str | None = None,
    cutoff_at: dt.datetime | None = None,
):
    stmt = select(RoastEvent).where(RoastEvent.date_str == date_str)
    if group_id:
        stmt = stmt.where(RoastEvent.group_id == group_id)
    if cutoff_at is not None:
        normalized_cutoff = database_cutoff_value(session, cutoff_at)
        stmt = stmt.where(RoastEvent.created_at <= normalized_cutoff)
    stmt = stmt.order_by(RoastEvent.created_at.asc(), RoastEvent.id.asc())
    rows = session.execute(stmt).scalars().all()
    if user_id:
        normalized_user_id = str(user_id)
        rows = [
            row
            for row in rows
            if row.attacker_id == normalized_user_id
            or row.target_id == normalized_user_id
            or normalized_user_id in {
                str(item) for item in (row.participant_snapshot or {}).get("ids", [])
            }
            or str((row.participant_snapshot or {}).get("backfire_victim_id", "")) == normalized_user_id
        ]
    return EventListResponse(
        items=[
            EventItem(
                event_id=str(row.id),
                created_at=database_datetime_for_response(session, row.created_at),
                type=row.event_type,
                attacker=row.attacker_id,
                target=row.target_id,
                attacker_name=row.attacker_name,
                target_name=row.target_name,
                food=row.food_name,
                group_id=row.group_id,
                reservation_id=row.reservation_id,
                participant_ids=(row.participant_snapshot or {}).get("ids", []),
                participant_names=(row.participant_snapshot or {}).get("names", []),
                participant_count=int((row.participant_snapshot or {}).get("count", 0)),
                backfire_victim_id=(row.participant_snapshot or {}).get("backfire_victim_id", ""),
                backfire_victim_name=(row.participant_snapshot or {}).get("backfire_victim_name", ""),
                special_reason=(row.participant_snapshot or {}).get("special_reason", ""),
            )
            for row in rows
        ]
    )
