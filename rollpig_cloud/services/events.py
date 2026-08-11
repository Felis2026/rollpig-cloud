from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import rollpig_today
from ..models import RoastEvent
from ..schemas import EventCreateRequest
from .roast_refills import mark_group_active_users


# ================================ 事件原子写入 ================================ #

def record_roast_event(session: Session, req: EventCreateRequest) -> bool:
    """写入烧烤事件；预约事件按 reservation_id 幂等，并同步登记群日活。"""

    target_date = req.date_str or rollpig_today()
    if req.reservation_id:
        existing = session.execute(
            select(RoastEvent.id).where(RoastEvent.reservation_id == req.reservation_id).limit(1)
        ).first()
        if existing is not None:
            return True

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
            reservation_id=req.reservation_id,
            participant_snapshot={
                "ids": req.participant_ids,
                "names": req.participant_names,
                "count": req.participant_count,
                "backfire_victim_id": req.backfire_victim_id,
                "backfire_victim_name": req.backfire_victim_name,
            } if req.reservation_id else None,
        )
    )
    if req.group_id:
        active_user_ids = [req.attacker_id, *req.participant_ids]
        if req.event_type != "bot_backfire":
            active_user_ids.append(req.target_id)
        mark_group_active_users(
            session,
            date_str=target_date,
            group_id=req.group_id,
            user_ids=active_user_ids,
        )
    return True
