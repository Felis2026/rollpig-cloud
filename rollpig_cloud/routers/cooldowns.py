from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..models import UserUsage
from ..schemas import ConsumeForceRequest, ConsumeRoastRequest, ConsumeRoastResponse, SimpleAllowedResponse

router = APIRouter(prefix="/v1/cooldowns", tags=["cooldowns"], dependencies=[Depends(verify_token)])


@router.post("/consume-roast", response_model=ConsumeRoastResponse)
def consume_roast(req: ConsumeRoastRequest, session: Session = Depends(get_session)):
    now_ts = float(req.now_ts or time.time())
    cooldown_seconds = max(1, int(req.cooldown_seconds or 8 * 3600))

    for _ in range(2):
        usage = session.execute(
            select(UserUsage).where(UserUsage.user_id == req.user_id).with_for_update()
        ).scalar_one_or_none()
        if usage is None:
            try:
                session.add(UserUsage(user_id=req.user_id, last_roast_ts=now_ts))
                session.commit()
                return ConsumeRoastResponse(allowed=True, remaining_seconds=0)
            except IntegrityError:
                session.rollback()
                continue

        last_use = float(usage.last_roast_ts or 0)
        remaining = max(0, int(cooldown_seconds - (now_ts - last_use)))
        if remaining > 0:
            session.rollback()
            return ConsumeRoastResponse(allowed=False, remaining_seconds=remaining)

        usage.last_roast_ts = now_ts
        session.commit()
        return ConsumeRoastResponse(allowed=True, remaining_seconds=0)

    raise RuntimeError("consume roast cooldown retry exhausted")


@router.post("/consume-force", response_model=SimpleAllowedResponse)
def consume_force(req: ConsumeForceRequest, session: Session = Depends(get_session)):
    for _ in range(2):
        usage = session.execute(
            select(UserUsage).where(UserUsage.user_id == req.user_id).with_for_update()
        ).scalar_one_or_none()
        if usage is None:
            try:
                session.add(UserUsage(user_id=req.user_id, last_force_date=req.date_str))
                session.commit()
                return SimpleAllowedResponse(allowed=True)
            except IntegrityError:
                session.rollback()
                continue

        if usage.last_force_date == req.date_str:
            session.rollback()
            return SimpleAllowedResponse(allowed=False)

        usage.last_force_date = req.date_str
        session.commit()
        return SimpleAllowedResponse(allowed=True)

    raise RuntimeError("consume force usage retry exhausted")
