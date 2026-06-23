from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import Collection, UserDrawState, UserPigProgress
from ..schemas import DailyRollLookupResponse, DrawStateResponse, PigProgressItem


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
def apply_created_roll_progress(session: Session, user_id: str, pig_id: str) -> tuple[bool, int, int]:
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

    return is_new_pig, previous_copies, previous_duplicate_streak


def build_lookup_response(
    session: Session,
    *,
    user_id: str,
    pig_id: str | None,
    created: bool,
    is_new_pig: bool = False,
    previous_copies: int | None = None,
    previous_duplicate_streak: int | None = None,
) -> DailyRollLookupResponse:
    if not pig_id:
        return DailyRollLookupResponse(pig_id=None, created=created)

    progress = get_progress(session, user_id, pig_id)
    collection = get_collection(session, user_id, pig_id)
    draw_state = get_draw_state(session, user_id)
    copies = int(progress.copies) if progress else (1 if collection else 0)
    duplicate_streak = int(draw_state.duplicate_streak) if draw_state else 0
    return DailyRollLookupResponse(
        pig_id=pig_id,
        created=created,
        is_new_pig=is_new_pig,
        previous_copies=copies if previous_copies is None else int(previous_copies),
        copies=copies,
        previous_duplicate_streak=(
            duplicate_streak if previous_duplicate_streak is None else int(previous_duplicate_streak)
        ),
        duplicate_streak=duplicate_streak,
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
