from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select, update
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
from .protections import protection_source
from .roast_refills import mark_group_active_users


ROAST_RESERVATION_MAX_PARTICIPANTS = 12


def _locked_participants(session: Session, reservation_id: str) -> list[RoastReservationParticipant]:
    """在调用方持有场次锁时读取当前名单，并刷新会话中的贡献值。"""

    # 通知预读可能已建立旧事务快照；FOR UPDATE 使用当前数据，不能退回普通 SELECT。
    return session.scalars(
        select(RoastReservationParticipant)
        .where(RoastReservationParticipant.reservation_id == reservation_id)
        .order_by(RoastReservationParticipant.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()


def reservation_to_schema(
    session: Session,
    reservation: RoastReservation,
    *,
    participants: list[RoastReservationParticipant] | None = None,
) -> RoastReservationItem:
    """构造预约快照，可复用调用方已锁定的当前参与者名单。"""

    rows = participants
    if rows is None:
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
                contribution=row.contribution,
            )
            for row in rows
        ],
        delivery_bot_id=reservation.delivery_bot_id,
        force_mode=reservation.force_mode,
        status=reservation.status,
        outcome_snapshot=reservation.outcome_snapshot,
        daily_feed_results=reservation.daily_feed_results or [],
        claim_token=reservation.claim_token or "",
        protection_required=reservation.protection_required,
        protection_source=reservation.protection_source,
        force_operator_id=reservation.force_operator_id,
        force_operator_name=reservation.force_operator_name,
        force_feed_user_ids=reservation.force_feed_user_ids or [],
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
    # 保护记录同时充当同群同目标的场次锁，串行化创建、升级与成功次数读取。
    protection = session.scalar(select(GroupProtection).where(
        GroupProtection.protect_date == req.date_str,
        GroupProtection.group_id == req.group_id,
        GroupProtection.user_id == req.target_id,
    ).with_for_update().execution_options(populate_existing=True))
    if protection is None:
        legacy = session.scalar(select(GroupProtection).where(
            GroupProtection.protect_date == req.date_str,
            GroupProtection.group_id == "__all__",
            GroupProtection.user_id == req.target_id,
        ).with_for_update())
        if legacy is not None and req.supports_protection_gathering:
            # 旧全局保护仍生效，合烤等级按群分别保存。
            protection = GroupProtection(protect_date=req.date_str, group_id=req.group_id, user_id=req.target_id)
            session.add(protection)
            session.flush()
        elif legacy is not None:
            protection = legacy
    if protection is not None and (not req.supports_protection_gathering or req.protection_gathering_version < 2) and req.force_mode != "super":
        return RoastReservationPrepareResponse(status="protected")
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
            RoastReservation.active_slot == 1,
            (RoastReservation.status == "pending") | (RoastReservation.protection_required > 0) | (req.force_mode == "super"),
        )
        .with_for_update()
        # 回复入口可能已预读 pending 对象，取得行锁后须覆盖会话中的旧状态。
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    # 回复只允许加入绑定的 pending 预约，不能落入创建或即时烧烤路径。
    if expected_reservation_id is not None and (
        reservation is None
        or reservation.reservation_id != expected_reservation_id
        or (target_roll is not None and not reservation.protection_required)
        or reservation.status != "pending"
    ):
        return RoastReservationPrepareResponse(status="reservation_closed")
    if reservation:
        members = _locked_participants(session, reservation.reservation_id)
        if reservation.status != "pending":
            return RoastReservationPrepareResponse(
                status="reservation_busy", reservation=reservation_to_schema(session, reservation, participants=members),
            )
        if reservation.force_mode == "super":
            return RoastReservationPrepareResponse(
                status="already_joined" if (reservation.force_operator_id or reservation.owner_id) == req.attacker_id else "reservation_closed",
                reservation=reservation_to_schema(session, reservation, participants=members),
            )
        if req.force_mode == "super":
            if not req.supports_protection_gathering or req.protection_gathering_version < 2:
                return RoastReservationPrepareResponse(
                    status="reservation_closed", reservation=reservation_to_schema(session, reservation, participants=members),
                )
            # 权限接管不增加报名人数，先保存原队员的加餐资格再改变判定模式。
            reservation.force_feed_user_ids = [
                item.user_id for item in members if item.user_id != req.attacker_id
                and (reservation.force_mode is None or reservation.protection_required)
                and (not reservation.protection_required or item.contribution == 1)
            ]
            reservation.force_operator_id = req.attacker_id
            reservation.force_operator_name = req.attacker_name
            reservation.force_mode = "super"
            # 接管后的场次只允许新客户端领取，由声明能力的请求 Bot 负责投递。
            reservation.delivery_bot_id = req.delivery_bot_id
            mark_group_active_users(session, date_str=req.date_str, group_id=req.group_id, user_ids=[req.attacker_id])
            session.flush()
            arrived_pig_id = reservation.target_pig_id or (target_roll.pig_id if target_roll else "")
            if arrived_pig_id:
                _activate_reservation(session, reservation, arrived_pig_id)
            return RoastReservationPrepareResponse(status="reservation_joined", reservation=reservation_to_schema(session, reservation, participants=members))
        participant = next((item for item in members if item.user_id == req.attacker_id), None)
        boosted = bool(reservation.protection_required and req.force_mode == "normal")
        upgrading = participant is not None and boosted
        if participant and not (boosted and participant.contribution < 2) and req.force_mode != "super":
            return RoastReservationPrepareResponse(
                status="already_joined",
                reservation=reservation_to_schema(session, reservation, participants=members),
            )
        if participant is None and len(members) >= ROAST_RESERVATION_MAX_PARTICIPANTS:
            return RoastReservationPrepareResponse(
                status="reservation_full",
                reservation=reservation_to_schema(session, reservation, participants=members),
            )
        if boosted and not consume_force_usage(session, user_id=req.attacker_id, date_str=req.date_str):
            return RoastReservationPrepareResponse(status="force_denied")
        if participant is None:
            participant = RoastReservationParticipant(
                reservation_id=reservation.reservation_id,
                user_id=req.attacker_id,
                display_name=req.attacker_name,
                pig_id=req.attacker_pig_id,
                contribution=2 if boosted else 1,
            )
            session.add(participant)
            members.append(participant)
        elif boosted:
            participant.contribution = 2
        if req.force_mode == "super" and reservation.protection_required:
            reservation.force_mode = "super"
        mark_group_active_users(
            session,
            date_str=req.date_str,
            group_id=req.group_id,
            user_ids=[req.attacker_id],
        )
        session.flush()
        # 抽猪事务可能在通知快照建立后到场，锁定场次中保存的形态优先于旧抽取视图。
        arrived_pig_id = reservation.target_pig_id or (target_roll.pig_id if target_roll else "")
        if arrived_pig_id:
            _activate_reservation(session, reservation, arrived_pig_id)
        return RoastReservationPrepareResponse(
            status="reservation_upgraded" if upgrading else "reservation_joined",
            reservation=reservation_to_schema(session, reservation, participants=members),
        )

    is_protected = protection is not None
    protection_broken = is_protected and req.force_mode == "super"
    if target_roll and (not is_protected or (req.force_mode == "super" and not req.supports_protection_gathering)):
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
        protection_required=(1 + min(5, protection.base_level + protection.roast_success_count)
                             if protection is not None and req.supports_protection_gathering else 0),
        protection_source=protection_source(protection) if protection is not None else "legacy",
    )
    session.add(reservation)
    session.flush()
    session.add(
        RoastReservationParticipant(
            reservation_id=reservation.reservation_id,
            user_id=req.attacker_id,
            display_name=req.attacker_name,
            pig_id=req.attacker_pig_id,
            contribution=2 if is_protected and req.force_mode == "normal" else 1,
        )
    )
    mark_group_active_users(
        session,
        date_str=req.date_str,
        group_id=req.group_id,
        user_ids=[req.attacker_id],
    )
    session.flush()
    if target_roll is not None:
        _activate_reservation(session, reservation, target_roll.pig_id)
    return RoastReservationPrepareResponse(
        status="reservation_created",
        reservation=reservation_to_schema(session, reservation),
        cooldown=cooldown,
        protection_broken=protection_broken,
    )


def _activate_reservation(session: Session, reservation: RoastReservation, target_pig_id: str) -> bool:
    """目标已到场时检查集结条件，达标后冻结名单并进入待领取状态。"""

    reservation.target_pig_id = target_pig_id
    participants = _locked_participants(session, reservation.reservation_id)
    if reservation.protection_required and reservation.force_mode != "super":
        if sum(item.contribution for item in participants) < reservation.protection_required:
            return False
    reservation.status = "ready"
    reservation.ready_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return True


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
    activated = 0
    for reservation in reservations:
        activated += int(_activate_reservation(session, reservation, target_pig_id))
    return activated


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
