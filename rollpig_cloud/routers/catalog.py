from __future__ import annotations

import datetime as dt
import json

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..config import rollpig_today
from ..db import get_session
from ..models import DailyRoll, RoastEvent
from ..schemas import CatalogSnapshotResponse, RecentRollItem
from ..services.progress import build_draw_state_response
from ..services.roast_statistics import count_roasted_users

router = APIRouter(prefix="/v1/catalog-snapshot", tags=["catalog"], dependencies=[Depends(verify_token)])


@router.get("", response_model=CatalogSnapshotResponse)
def get_catalog_snapshot(user_id: str, days: int = 14, session: Session = Depends(get_session)):
    """返回图鉴渲染所需的只读聚合数据，避免 bot 端为一张图重复打多个接口。"""
    safe_days = max(1, min(60, int(days or 14)))
    today = rollpig_today()
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
    events = session.scalars(select(RoastEvent).where(
        RoastEvent.date_str >= roast_start_date, RoastEvent.date_str <= today,
        or_(RoastEvent.target_id == user_id, RoastEvent.attacker_id == user_id,
            # 旧预约只保存单人受害者，可能既不是主厨也不是原目标。
            RoastEvent.participant_snapshot["backfire_victim_id"].as_string() == user_id,
            RoastEvent.participant_snapshot["backfire_victim_ids"].as_string().contains(json.dumps(user_id), autoescape=True)),
    ).order_by(RoastEvent.created_at, RoastEvent.id)).all()
    roasted_7d = count_roasted_users({
        "event_id": str(row.id), "date_str": row.date_str.isoformat(), "group_id": row.group_id,
        "type": row.event_type, "attacker": row.attacker_id, "target": row.target_id,
        "backfire_victim_id": (row.participant_snapshot or {}).get("backfire_victim_id", ""),
        "backfire_victim_ids": (row.participant_snapshot or {}).get("backfire_victim_ids", []),
        "special_reason": (row.participant_snapshot or {}).get("special_reason", ""),
    } for row in events)[user_id]

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
