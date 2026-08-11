from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import rollpig_today
from ..models import RoastEvent, RoastReservation
from ..schemas import EventCreateRequest, RoastReservationItem
from .roast_refills import mark_group_active_users


# ================================ 预约事件身份绑定 ================================ #

def bind_reservation_event(
    reservation: RoastReservationItem,
    req: EventCreateRequest,
) -> EventCreateRequest:
    """以 Cloud 预约快照为身份真源，仅保留客户端计算出的结果字段。"""

    return req.model_copy(update={
        "date_str": reservation.date_str,
        "group_id": reservation.group_id,
        "attacker_id": reservation.owner_id,
        "attacker_name": reservation.owner_name,
        "target_id": reservation.target_id,
        "target_name": reservation.target_name,
        "reservation_id": reservation.reservation_id,
        "participant_ids": [item.user_id for item in reservation.participants],
        "participant_names": [item.display_name for item in reservation.participants],
        "participant_count": len(reservation.participants),
    })


# ================================ 事件原子写入 ================================ #

def record_roast_event(
    session: Session,
    req: EventCreateRequest,
    *,
    reservation: RoastReservationItem | None = None,
) -> bool:
    """写入烧烤事件；预约事件按 reservation_id 幂等，并同步登记群日活。"""

    if req.reservation_id:
        if reservation is None:
            reservation_row = session.execute(
                select(RoastReservation).where(
                    RoastReservation.reservation_id == req.reservation_id
                ).with_for_update()
            ).scalar_one_or_none()
            if reservation_row is not None:
                # 旧 Plus 会先 /complete、再单独调用 /events；这里回查预约记录，
                # 同时锁定预约，使并发重试在检查事件前串行化。
                from .reservations import reservation_to_schema

                reservation = reservation_to_schema(session, reservation_row)
        existing = session.execute(
            select(RoastEvent.id).where(RoastEvent.reservation_id == req.reservation_id).limit(1)
        ).first()
        if existing is not None:
            return True
        if reservation is not None:
            req = bind_reservation_event(reservation, req)

    target_date = req.date_str or rollpig_today()

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
