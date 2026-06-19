from __future__ import annotations

import time

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..migrations import DEFAULT_ROAST_CHARGE_MAX, DEFAULT_ROAST_CHARGE_RECOVER_SECONDS
from ..models import UserUsage
from ..schemas import ConsumeForceRequest, ConsumeRoastRequest, ConsumeRoastResponse, SimpleAllowedResponse

router = APIRouter(prefix="/v1/cooldowns", tags=["cooldowns"], dependencies=[Depends(verify_token)])


def _clamp_charge_settings(max_charges: int | None, recover_seconds: int | None) -> tuple[int, int]:
    """限制客户端传入值，避免异常实例把充能桶扩到不可控。"""
    safe_max = max(1, min(6, int(max_charges or DEFAULT_ROAST_CHARGE_MAX)))
    safe_recover = max(60, min(7 * 24 * 3600, int(recover_seconds or DEFAULT_ROAST_CHARGE_RECOVER_SECONDS)))
    return safe_max, safe_recover


def _state_from_legacy_last_use(last_roast_ts: int, now_ts: int, max_charges: int, recover_seconds: int) -> tuple[int, int]:
    """旧 last_roast_ts 只能表达单次 CD，这里按“最近一次使用后剩 1 格”宽松迁移。"""
    if last_roast_ts <= 0:
        return max_charges, now_ts
    elapsed = max(0, now_ts - last_roast_ts)
    recovered = elapsed // recover_seconds
    charges = min(max_charges, 1 + recovered)
    updated_ts = now_ts if charges >= max_charges else last_roast_ts + recovered * recover_seconds
    return int(charges), int(updated_ts)


def _recover_charges(charges: int, updated_ts: int, now_ts: int, max_charges: int, recover_seconds: int) -> tuple[int, int]:
    """按 token bucket 规则恢复充能；满格时把时间锚点归到 now，避免未来时间残留。"""
    charges = max(0, min(max_charges, int(charges)))
    updated_ts = int(updated_ts or now_ts)
    if charges >= max_charges:
        return max_charges, now_ts
    elapsed = max(0, now_ts - updated_ts)
    recovered = elapsed // recover_seconds
    if recovered <= 0:
        return charges, updated_ts
    charges = min(max_charges, charges + recovered)
    updated_ts = now_ts if charges >= max_charges else updated_ts + recovered * recover_seconds
    return int(charges), int(updated_ts)


def _next_recover_seconds(charges: int, updated_ts: int, now_ts: int, max_charges: int, recover_seconds: int) -> int:
    if charges >= max_charges:
        return 0
    elapsed = max(0, now_ts - int(updated_ts or now_ts))
    return max(1, int(recover_seconds - (elapsed % recover_seconds)))


@router.post("/consume-roast", response_model=ConsumeRoastResponse)
def consume_roast(req: ConsumeRoastRequest, session: Session = Depends(get_session)):
    # ================================ 整数秒时间戳归一化 ================================ #
    # MySQL FLOAT 在保存 10 位 Unix 时间戳时会发生精度丢失，进一步导致
    # SQLAlchemy 读回来的值偏大，最终把 8h CD 算成 9h+。
    # 这里统一改用整数秒，和数据库 BIGINT 列配套，彻底消除精度问题。
    now_ts = int(float(req.now_ts or time.time()))
    max_charges, recover_seconds = _clamp_charge_settings(req.max_charges, req.cooldown_seconds)

    for _ in range(2):
        usage = session.execute(
            select(UserUsage).where(UserUsage.user_id == req.user_id).with_for_update()
        ).scalar_one_or_none()
        if usage is None:
            try:
                charges_left = max(0, max_charges - 1)
                session.add(
                    UserUsage(
                        user_id=req.user_id,
                        last_roast_ts=now_ts,
                        roast_charges=charges_left,
                        roast_charge_updated_ts=now_ts,
                    )
                )
                session.commit()
                return ConsumeRoastResponse(
                    allowed=True,
                    remaining_seconds=0,
                    charges_left=charges_left,
                    max_charges=max_charges,
                    next_recover_seconds=(
                        _next_recover_seconds(charges_left, now_ts, now_ts, max_charges, recover_seconds)
                    ),
                )
            except IntegrityError:
                session.rollback()
                continue

        if usage.roast_charges is None or usage.roast_charge_updated_ts is None:
            charges, updated_ts = _state_from_legacy_last_use(
                int(usage.last_roast_ts or 0),
                now_ts,
                max_charges,
                recover_seconds,
            )
        else:
            charges, updated_ts = int(usage.roast_charges or 0), int(usage.roast_charge_updated_ts or now_ts)

        charges, updated_ts = _recover_charges(charges, updated_ts, now_ts, max_charges, recover_seconds)
        if charges <= 0:
            remaining = _next_recover_seconds(charges, updated_ts, now_ts, max_charges, recover_seconds)
            usage.roast_charges = charges
            usage.roast_charge_updated_ts = updated_ts
            session.commit()
            return ConsumeRoastResponse(
                allowed=False,
                remaining_seconds=remaining,
                charges_left=0,
                max_charges=max_charges,
                next_recover_seconds=remaining,
            )

        was_full = charges >= max_charges
        charges -= 1
        if was_full:
            updated_ts = now_ts
        usage.last_roast_ts = now_ts
        usage.roast_charges = charges
        usage.roast_charge_updated_ts = updated_ts
        session.commit()
        return ConsumeRoastResponse(
            allowed=True,
            remaining_seconds=0,
            charges_left=charges,
            max_charges=max_charges,
            next_recover_seconds=_next_recover_seconds(charges, updated_ts, now_ts, max_charges, recover_seconds),
        )

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
