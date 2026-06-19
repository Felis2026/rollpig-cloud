from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..models import DailyRoll, RoastEvent
from ..schemas import CatalogSnapshotResponse, RecentRollItem
from ..services.progress import build_draw_state_response

router = APIRouter(prefix="/v1/catalog-snapshot", tags=["catalog"], dependencies=[Depends(verify_token)])


@router.get("", response_model=CatalogSnapshotResponse)
def get_catalog_snapshot(user_id: str, days: int = 14, session: Session = Depends(get_session)):
    """返回图鉴渲染所需的只读聚合数据，避免 bot 端为一张图重复打多个接口。"""
    safe_days = max(1, min(60, int(days or 14)))
    today = dt.date.today()
    start_date = today - dt.timedelta(days=safe_days - 1)
    roast_start_date = today - dt.timedelta(days=6)

    draw_state = build_draw_state_response(session, user_id)
    recent_roll_rows = (
        session.execute(
            select(DailyRoll)
            .where(DailyRoll.user_id == user_id, DailyRoll.date_str >= start_date)
            .order_by(DailyRoll.date_str.desc())
        )
        .scalars()
        .all()
    )
    roasted_7d = int(
        session.execute(
            select(func.count(RoastEvent.id)).where(
                RoastEvent.target_id == user_id,
                RoastEvent.date_str >= roast_start_date,
                RoastEvent.event_type == "success",
            )
        ).scalar()
        or 0
    )

    return CatalogSnapshotResponse(
        pig_ids=draw_state.pig_ids,
        progress=draw_state.progress,
        duplicate_streak=draw_state.duplicate_streak,
        recent_rolls=[
            RecentRollItem(date_str=row.date_str, pig_id=row.pig_id)
            for row in recent_roll_rows
        ],
        roasted_7d=roasted_7d,
        roast_events_7d=roasted_7d,
    )
