from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import GroupProtection, RoastReservation, RoastReservationParticipant


def protection_sources(row: GroupProtection) -> dict[str, int]:
    """读取分来源等级，缺少来源的历史记录保留为 legacy。"""
    return dict(row.source_levels) if row.source_levels is not None else {"legacy": row.base_level}


def protection_source(row: GroupProtection) -> str:
    """按最高来源等级选择文案，同级优先使用日报池。"""
    levels = {source: min(5, level + row.roast_success_count) for source, level in protection_sources(row).items()}
    highest = max(levels.values(), default=0)
    return next((source for source in ("daily", "manual", "pig", "legacy") if levels.get(source) == highest), "legacy")


def sync_pending_protection(session: Session, row: GroupProtection) -> None:
    """更新等待中场次的保护门槛，已开炉的场次保留固定条件。"""
    pending = session.scalars(select(RoastReservation).where(
        RoastReservation.date_str == row.protect_date,
        RoastReservation.group_id == row.group_id,
        RoastReservation.target_id == row.user_id,
        RoastReservation.status == "pending",
    ).with_for_update().execution_options(populate_existing=True)).all()
    for reservation in pending:
        if reservation.force_mode == "super":
            continue
        if not reservation.protection_required and reservation.force_mode == "normal":
            # 普通加急预约转为保护合围时，已消费的加急折算为双倍火力。
            owner = session.scalar(select(RoastReservationParticipant).where(
                RoastReservationParticipant.reservation_id == reservation.reservation_id,
                RoastReservationParticipant.user_id == reservation.owner_id,
            ).with_for_update().execution_options(populate_existing=True))
            if owner is not None:
                owner.contribution = 2
        reservation.protection_required = 1 + min(5, row.base_level + row.roast_success_count)
        reservation.protection_source = protection_source(row)
        if reservation.target_pig_id:
            from .reservations import _activate_reservation

            # 激活会重新读取参与者，先保存已折算的加急火力。
            session.flush()
            _activate_reservation(session, reservation, reservation.target_pig_id)
