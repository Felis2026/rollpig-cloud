from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..models import DailyRoll, GroupProtection, RoastReservation, RoastReservationParticipant
from ..schemas import (
    ConsumeRoastResponse,
    RoastReservationItem,
    RoastReservationParticipantItem,
    RoastReservationPrepareRequest,
    RoastReservationPrepareResponse,
)
from .usage import consume_force_usage, consume_roast_usage
from .roast_refills import mark_group_active_users


ROAST_RESERVATION_MAX_PARTICIPANTS = 12


def reservation_to_schema(session: Session, reservation: RoastReservation) -> RoastReservationItem:
    """构造完整预约快照；参与者按加入顺序稳定返回。"""

    rows = session.execute(
        select(RoastReservationParticipant)
        .where(RoastReservationParticipant.reservation_id == reservation.reservation_id)
        .order_by(RoastReservationParticipant.id)
    ).scalars().all()
    return RoastReservationItem(
        reservation_id=reservation.reservation_id,
        date_str=reservation.date_str,
        group_id=reservation.group_id,
        target_id=reservation.target_id,
        target_name=reservation.target_name,
        target_pig_id=reservation.target_pig_id,
        owner_id=reservation.owner_id,
        owner_name=reservation.owner_name,
        owner_pig_id=reservation.owner_pig_id,
        participants=[
            RoastReservationParticipantItem(
                user_id=row.user_id,
                display_name=row.display_name,
                pig_id=row.pig_id,
            )
            for row in rows
        ],
        delivery_bot_id=reservation.delivery_bot_id,
        force_mode=reservation.force_mode,
        status=reservation.status,
        outcome_snapshot=reservation.outcome_snapshot,
        daily_feed_results=reservation.daily_feed_results or [],
        claim_token=reservation.claim_token or "",
    )


def prepare_reservation(
    session: Session,
    req: RoastReservationPrepareRequest,
    *,
    expected_reservation_id: str | None = None,
) -> RoastReservationPrepareResponse:
    """在单一事务内检查目标、加入现有预约或消费资源创建预约。"""

    if session.get_bind().dialect.name == "sqlite":
        # SQLite 忽略 FOR UPDATE；先取得写锁，再计数，避免两种加入入口并发超员。
        session.execute(update(RoastReservation).where(
            RoastReservation.date_str == req.date_str,
            RoastReservation.group_id == req.group_id,
            RoastReservation.target_id == req.target_id,
        ).values(status=RoastReservation.status))
    target_roll = session.execute(
        select(DailyRoll).where(DailyRoll.date_str == req.date_str, DailyRoll.user_id == req.target_id)
    ).scalar_one_or_none()
    # 补签不代表目标昨天到场，不能放行普通烧烤或触发历史预约。
    if target_roll is not None and (target_roll.appearance_snapshot or {}).get("is_makeup") is True:
        target_roll = None
    reservation = session.execute(
        select(RoastReservation)
        .where(
            RoastReservation.date_str == req.date_str,
            RoastReservation.group_id == req.group_id,
            RoastReservation.target_id == req.target_id,
            RoastReservation.status == "pending",
        )
        .with_for_update()
    ).scalar_one_or_none()
    # 回复只允许加入绑定的 pending 预约，不能落入创建或即时烧烤路径。
    if expected_reservation_id is not None and (
        reservation is None
        or reservation.reservation_id != expected_reservation_id
        or target_roll is not None
    ):
        return RoastReservationPrepareResponse(status="reservation_closed")
    if reservation:
        participant = session.execute(
            select(RoastReservationParticipant).where(
                RoastReservationParticipant.reservation_id == reservation.reservation_id,
                RoastReservationParticipant.user_id == req.attacker_id,
            )
        ).scalar_one_or_none()
        if participant:
            return RoastReservationPrepareResponse(
                status="already_joined",
                reservation=reservation_to_schema(session, reservation),
            )
        participant_count = session.scalar(
            select(func.count()).select_from(RoastReservationParticipant).where(
                RoastReservationParticipant.reservation_id == reservation.reservation_id
            )
        ) or 0
        if participant_count >= ROAST_RESERVATION_MAX_PARTICIPANTS:
            return RoastReservationPrepareResponse(
                status="reservation_full",
                reservation=reservation_to_schema(session, reservation),
            )
        session.add(
            RoastReservationParticipant(
                reservation_id=reservation.reservation_id,
                user_id=req.attacker_id,
                display_name=req.attacker_name,
                pig_id=req.attacker_pig_id,
            )
        )
        mark_group_active_users(
            session,
            date_str=req.date_str,
            group_id=req.group_id,
            user_ids=[req.attacker_id],
        )
        session.flush()
        return RoastReservationPrepareResponse(
            status="reservation_joined",
            reservation=reservation_to_schema(session, reservation),
        )

    is_protected = session.execute(
        select(GroupProtection.id).where(
            GroupProtection.protect_date == req.date_str,
            GroupProtection.group_id == req.group_id,
            GroupProtection.user_id == req.target_id,
        ).limit(1)
    ).first() is not None
    protection_broken = is_protected and req.force_mode in {"normal", "super"}
    if is_protected and not protection_broken:
        return RoastReservationPrepareResponse(status="protected")
    if target_roll:
        return RoastReservationPrepareResponse(
            status="target_ready",
            target_pig_id=target_roll.pig_id,
            protection_broken=protection_broken,
        )

    cooldown: ConsumeRoastResponse | None = None
    if req.force_mode == "normal":
        if not consume_force_usage(session, user_id=req.attacker_id, date_str=req.date_str):
            return RoastReservationPrepareResponse(status="force_denied")
    elif req.force_mode != "super":
        cooldown = consume_roast_usage(
            session,
            user_id=req.attacker_id,
            now_ts=req.now_ts,
            cooldown_seconds=req.cooldown_seconds,
            max_charges=req.max_charges,
        )
        if not cooldown.allowed:
            return RoastReservationPrepareResponse(status="cooldown_denied", cooldown=cooldown)

    reservation = RoastReservation(
        reservation_id=uuid.uuid4().hex,
        date_str=req.date_str,
        group_id=req.group_id,
        target_id=req.target_id,
        target_name=req.target_name,
        owner_id=req.attacker_id,
        owner_name=req.attacker_name,
        owner_pig_id=req.attacker_pig_id,
        delivery_bot_id=req.delivery_bot_id,
        force_mode=req.force_mode,
        status="pending",
    )
    session.add(reservation)
    session.flush()
    session.add(
        RoastReservationParticipant(
            reservation_id=reservation.reservation_id,
            user_id=req.attacker_id,
            display_name=req.attacker_name,
            pig_id=req.attacker_pig_id,
        )
    )
    mark_group_active_users(
        session,
        date_str=req.date_str,
        group_id=req.group_id,
        user_ids=[req.attacker_id],
    )
    session.flush()
    return RoastReservationPrepareResponse(
        status="reservation_created",
        reservation=reservation_to_schema(session, reservation),
        cooldown=cooldown,
        protection_broken=protection_broken,
    )


def activate_target_reservations(
    session: Session,
    *,
    date_str: dt.date,
    target_id: str,
    target_pig_id: str,
) -> int:
    """把目标当天所有 pending 预约原子切换为 ready；重复调用保持幂等。"""

    # 在状态转换入口再守一次，覆盖抽取侧对账和直接调用者。
    target_roll = session.execute(
        select(DailyRoll).where(DailyRoll.date_str == date_str, DailyRoll.user_id == target_id)
    ).scalar_one_or_none()
    if target_roll is not None and (target_roll.appearance_snapshot or {}).get("is_makeup") is True:
        return 0

    reservations = session.execute(
        select(RoastReservation)
        .where(
            RoastReservation.date_str == date_str,
            RoastReservation.target_id == target_id,
            RoastReservation.status == "pending",
        )
        .with_for_update()
    ).scalars().all()
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    for reservation in reservations:
        reservation.status = "ready"
        reservation.target_pig_id = target_pig_id
        reservation.ready_at = now
    return len(reservations)


def activate_if_target_already_rolled(
    session: Session,
    *,
    date_str: dt.date,
    target_id: str,
) -> int:
    """预约提交后做一次对账，封住“DailyRoll 先提交、预约后提交”的竞态窗口。"""

    target_roll = session.execute(
        select(DailyRoll).where(DailyRoll.date_str == date_str, DailyRoll.user_id == target_id)
    ).scalar_one_or_none()
    if target_roll is None or (target_roll.appearance_snapshot or {}).get("is_makeup") is True:
        return 0
    return activate_target_reservations(
        session,
        date_str=date_str,
        target_id=target_id,
        target_pig_id=target_roll.pig_id,
    )
