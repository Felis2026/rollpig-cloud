from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Collection, DailyRoll, UserDrawState, UserPigProgress
from ..schemas import DailyRollLookupResponse, DailyRollOutcomeSnapshot, DrawStateResponse, PigProgressItem


@dataclass(frozen=True)
class CreatedRollProgress:
    """一次首次抽取事务产生的历史结果；写入 DailyRoll 后不得再由当前进度倒推。"""

    is_new_pig: bool
    previous_copies: int
    copies_after_roll: int
    collection_size_after_roll: int
    previous_duplicate_streak: int
    duplicate_streak_after_roll: int


def get_collection(session: Session, user_id: str, pig_id: str) -> Collection | None:
    return session.execute(
        select(Collection).where(Collection.user_id == user_id, Collection.pig_id == pig_id)
    ).scalar_one_or_none()


def ensure_collection(session: Session, user_id: str, pig_id: str) -> Collection:
    exists = get_collection(session, user_id, pig_id)
    if exists:
        return exists
    created = Collection(user_id=user_id, pig_id=pig_id)
    session.add(created)
    return created


def get_progress(session: Session, user_id: str, pig_id: str, *, for_update: bool = False) -> UserPigProgress | None:
    stmt = select(UserPigProgress).where(
        UserPigProgress.tenant_id == settings.default_tenant_id,
        UserPigProgress.user_id == user_id,
        UserPigProgress.pig_id == pig_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


def get_draw_state(session: Session, user_id: str, *, for_update: bool = False) -> UserDrawState | None:
    stmt = select(UserDrawState).where(
        UserDrawState.tenant_id == settings.default_tenant_id,
        UserDrawState.user_id == user_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return session.execute(stmt).scalar_one_or_none()


# ================================ P1A抽猪成长状态 ================================ #
# 这里是 P1A 的唯一写入收口：只有当天 DailyRoll 首次创建成功时，才允许更新 copies
# 与 duplicate_streak。重复发送“今日小猪”只读取既有结果，绝不能刷等级。
def apply_created_roll_progress(session: Session, user_id: str, pig_id: str) -> CreatedRollProgress:
    collection = get_collection(session, user_id, pig_id)
    progress = get_progress(session, user_id, pig_id, for_update=True)
    draw_state = get_draw_state(session, user_id, for_update=True)
    previous_duplicate_streak = int(draw_state.duplicate_streak) if draw_state else 0
    is_new_pig = collection is None and progress is None

    if is_new_pig:
        ensure_collection(session, user_id, pig_id)
        previous_copies = 0
        copies = 1
        duplicate_streak = 0
        session.add(
            UserPigProgress(
                tenant_id=settings.default_tenant_id,
                user_id=user_id,
                pig_id=pig_id,
                copies=copies,
            )
        )
    else:
        ensure_collection(session, user_id, pig_id)
        previous_copies = int(progress.copies) if progress else 1
        copies = previous_copies + 1
        duplicate_streak = previous_duplicate_streak + 1
        if progress:
            progress.copies = copies
        else:
            first_obtained_at = (
                collection.first_seen_at if collection else dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            )
            session.add(
                UserPigProgress(
                    tenant_id=settings.default_tenant_id,
                    user_id=user_id,
                    pig_id=pig_id,
                    copies=copies,
                    first_obtained_at=first_obtained_at,
                )
            )

    if draw_state:
        draw_state.duplicate_streak = duplicate_streak
    else:
        session.add(
            UserDrawState(
                tenant_id=settings.default_tenant_id,
                user_id=user_id,
                duplicate_streak=duplicate_streak,
            )
        )

    # flush 后再统计，确保本次刚加入的 Collection 已包含在历史快照中。
    session.flush()
    collection_size = int(session.scalar(
        select(func.count(Collection.id)).where(Collection.user_id == user_id)
    ) or 0)
    return CreatedRollProgress(
        is_new_pig=is_new_pig,
        previous_copies=previous_copies,
        copies_after_roll=copies,
        collection_size_after_roll=collection_size,
        previous_duplicate_streak=previous_duplicate_streak,
        duplicate_streak_after_roll=duplicate_streak,
    )


def build_lookup_response(
    session: Session,
    *,
    daily_roll: DailyRoll | None,
    created: bool,
) -> DailyRollLookupResponse:
    if daily_roll is None:
        return DailyRollLookupResponse(pig_id=None, created=created)

    user_id = daily_roll.user_id
    pig_id = daily_roll.pig_id
    progress = get_progress(session, user_id, pig_id)
    collection = get_collection(session, user_id, pig_id)
    draw_state = get_draw_state(session, user_id)
    copies = int(progress.copies) if progress else (1 if collection else 0)
    duplicate_streak = int(draw_state.duplicate_streak) if draw_state else 0

    growth_snapshot_available = all(
        value is not None
        for value in (
            daily_roll.is_new_pig,
            daily_roll.previous_copies,
            daily_roll.copies_after_roll,
            daily_roll.collection_size_after_roll,
            daily_roll.previous_duplicate_streak,
            daily_roll.duplicate_streak_after_roll,
        )
    )
    if growth_snapshot_available:
        appearance_snapshot_available = bool(daily_roll.resource_version) and isinstance(
            daily_roll.appearance_snapshot,
            dict,
        )
        appearance = daily_roll.appearance_snapshot if appearance_snapshot_available else {}
        outcome_snapshot = DailyRollOutcomeSnapshot(
            snapshot_available=appearance_snapshot_available,
            collection_size_after_roll=int(daily_roll.collection_size_after_roll or 0),
            resource_version=(
                str(daily_roll.resource_version)
                if appearance_snapshot_available
                else ""
            ),
            resolved_variant_level=int(appearance.get("resolved_variant_level") or 0),
            resolved_image_name=str(appearance.get("resolved_image_name") or ""),
            unlocked_variant_levels=[int(level) for level in appearance.get("unlocked_variant_levels", [])],
            unlocked_variant_fields=[str(field) for field in appearance.get("unlocked_variant_fields", [])],
        )
        response_is_new = bool(daily_roll.is_new_pig)
        response_previous_copies = int(daily_roll.previous_copies or 0)
        response_copies = int(daily_roll.copies_after_roll or 0)
        response_previous_duplicate_streak = int(daily_roll.previous_duplicate_streak or 0)
        response_duplicate_streak = int(daily_roll.duplicate_streak_after_roll or 0)
    else:
        # 旧行没有历史快照时维持原平铺字段行为，供旧 Plus 继续使用；
        # 新 Plus 只认 outcome_snapshot，不会把当前进度冒充昨日结果。
        outcome_snapshot = None
        response_is_new = False
        response_previous_copies = copies
        response_copies = copies
        response_previous_duplicate_streak = duplicate_streak
        response_duplicate_streak = duplicate_streak

    return DailyRollLookupResponse(
        pig_id=pig_id,
        created=created,
        is_new_pig=response_is_new,
        previous_copies=response_previous_copies,
        copies=response_copies,
        previous_duplicate_streak=response_previous_duplicate_streak,
        duplicate_streak=response_duplicate_streak,
        outcome_snapshot=outcome_snapshot,
    )


def build_draw_state_response(session: Session, user_id: str) -> DrawStateResponse:
    # ================================ 图鉴成长状态聚合 ================================ #
    # 旧数据可能只有 collections，没有 user_pig_progress。这里读接口做兼容聚合：
    # progress 存在时以 progress 为准；缺失时用 collection 兜底为 copies=1。
    progress_rows = session.execute(
        select(UserPigProgress).where(
            UserPigProgress.tenant_id == settings.default_tenant_id,
            UserPigProgress.user_id == user_id,
        )
    ).scalars().all()
    collection_rows = session.execute(
        select(Collection).where(Collection.user_id == user_id)
    ).scalars().all()
    draw_state = get_draw_state(session, user_id)

    progress = {
        row.pig_id: PigProgressItem(copies=int(row.copies), first_obtained_at=row.first_obtained_at)
        for row in progress_rows
    }
    for row in collection_rows:
        progress.setdefault(
            row.pig_id,
            PigProgressItem(copies=1, first_obtained_at=row.first_seen_at),
        )

    return DrawStateResponse(
        pig_ids=sorted(progress),
        progress=dict(sorted(progress.items())),
        duplicate_streak=int(draw_state.duplicate_streak) if draw_state else 0,
    )
