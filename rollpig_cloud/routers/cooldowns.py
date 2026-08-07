from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..schemas import ConsumeForceRequest, ConsumeRoastRequest, ConsumeRoastResponse, SimpleAllowedResponse
from ..services.usage import consume_force_usage, consume_roast_usage

router = APIRouter(prefix="/v1/cooldowns", tags=["cooldowns"], dependencies=[Depends(verify_token)])


@router.post("/consume-roast", response_model=ConsumeRoastResponse)
def consume_roast(req: ConsumeRoastRequest, session: Session = Depends(get_session)):
    # ================================ 整数秒时间戳归一化 ================================ #
    # MySQL FLOAT 在保存 10 位 Unix 时间戳时会发生精度丢失，进一步导致
    # SQLAlchemy 读回来的值偏大，最终把 8h CD 算成 9h+。
    # 这里统一改用整数秒，和数据库 BIGINT 列配套，彻底消除精度问题。
    for _ in range(2):
        try:
            result = consume_roast_usage(
                session,
                user_id=req.user_id,
                now_ts=req.now_ts,
                cooldown_seconds=req.cooldown_seconds,
                max_charges=req.max_charges,
            )
            session.commit()
            return result
        except IntegrityError:
            session.rollback()
            continue

    raise RuntimeError("consume roast cooldown retry exhausted")


@router.post("/consume-force", response_model=SimpleAllowedResponse)
def consume_force(req: ConsumeForceRequest, session: Session = Depends(get_session)):
    for _ in range(2):
        try:
            allowed = consume_force_usage(session, user_id=req.user_id, date_str=req.date_str)
            session.commit()
            return SimpleAllowedResponse(allowed=allowed)
        except IntegrityError:
            session.rollback()
            continue

    raise RuntimeError("consume force usage retry exhausted")
