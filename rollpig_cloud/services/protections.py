from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import GroupProtection, RoastReservation, RoastReservationParticipant


def protection_sources(row: GroupProtection) -> dict[str, int]:
    """读取分来源等级，缺少来源的历史记录保留为 legacy。"""
    return dict(row.source_levels) if row.source_levels is not None else {"legacy": row.base_level}


def protection_source(row: GroupProtection) -> str:
    """按最高来源等级选择文案，同级优先使用日报池。"""
    levels = protection_levels(row)
    highest = max(levels.values(), default=0)
    return next((source for source in ("daily", "manual", "pig", "legacy") if levels.get(source) == highest), "legacy")


def protection_levels(row: GroupProtection) -> dict[str, int]:
    """按各来源获得保护时的破防基准计算当前等级，旧来源基准为零。"""
    baselines = row.source_baselines or {}
    return {source: min(5, level + max(0, row.roast_success_count - int(baselines.get(source, 0))))
            for source, level in protection_sources(row).items()}


def protection_level(row: GroupProtection) -> int:
    """返回多来源保护当前实际生效的最高等级。"""
    return max(protection_levels(row).values(), default=0)


def inherit_global_protection(row: GroupProtection, group_id: str) -> GroupProtection:
    """生成未入库的群内全局保护副本，按当前等级继承，本群破防和日报连续天数从零计。"""
    level = protection_level(row)
    return GroupProtection(protect_date=row.protect_date, group_id=group_id, user_id=row.user_id,
                           base_level=level, roast_success_count=0, consecutive_days=0,
                           source_levels={"legacy": level}, source_baselines={"legacy": 0})


def is_historical_daily_protection(row: GroupProtection) -> bool:
    """按来源和连续天数识别尚未标为 daily 的历史日报保护。"""
    sources = protection_sources(row)
    return "legacy" in sources and "daily" not in sources and row.consecutive_days > 0


def sync_pending_protection(session: Session, row: GroupProtection, *, removed: bool = False, group_id: str | None = None) -> None:
    """同步等待中场次的门槛，removed 为真时恢复普通预约。

    group_id 用于将旧全局保护应用到指定群；已开炉场次保留固定条件。
    """
    pending = session.scalars(select(RoastReservation).where(
        RoastReservation.date_str == row.protect_date,
        RoastReservation.group_id == (group_id or row.group_id),
        RoastReservation.target_id == row.user_id,
        RoastReservation.status == "pending",
    ).with_for_update().execution_options(populate_existing=True)).all()
    for reservation in pending:
        if reservation.force_mode == "super":
            continue
        if not removed and not reservation.protection_required and reservation.force_mode == "normal":
            # 普通加急预约转为保护合围时，已消费的加急折算为双倍火力。
            owner = session.scalar(select(RoastReservationParticipant).where(
                RoastReservationParticipant.reservation_id == reservation.reservation_id,
                RoastReservationParticipant.user_id == reservation.owner_id,
            ).with_for_update().execution_options(populate_existing=True))
            if owner is not None:
                owner.contribution = 2
        reservation.protection_required = 0 if removed else 1 + protection_level(row)
        reservation.protection_source = "legacy" if removed else protection_source(row)
        if reservation.target_pig_id:
            from .reservations import _activate_reservation

            # 激活会重新读取参与者，先保存已折算的加急火力。
            session.flush()
            _activate_reservation(session, reservation, reservation.target_pig_id)
