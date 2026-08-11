from __future__ import annotations

import datetime as dt
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..migrations import DEFAULT_ROAST_CHARGE_MAX, DEFAULT_ROAST_CHARGE_RECOVER_SECONDS
from ..models import UserUsage
from ..schemas import ConsumeRoastResponse


def clamp_charge_settings(max_charges: int | None, recover_seconds: int | None) -> tuple[int, int]:
    """限制客户端传入值，避免异常实例把充能桶扩到不可控。"""

    safe_max = max(1, min(6, int(max_charges or DEFAULT_ROAST_CHARGE_MAX)))
    safe_recover = max(60, min(7 * 24 * 3600, int(recover_seconds or DEFAULT_ROAST_CHARGE_RECOVER_SECONDS)))
    return safe_max, safe_recover


def _state_from_legacy_last_use(last_roast_ts: int, now_ts: int, max_charges: int, recover_seconds: int) -> tuple[int, int]:
    if last_roast_ts <= 0:
        return max_charges, now_ts
    elapsed = max(0, now_ts - last_roast_ts)
    recovered = elapsed // recover_seconds
    charges = min(max_charges, 1 + recovered)
    updated_ts = now_ts if charges >= max_charges else last_roast_ts + recovered * recover_seconds
    return int(charges), int(updated_ts)


def _recover_charges(charges: int, updated_ts: int, now_ts: int, max_charges: int, recover_seconds: int) -> tuple[int, int]:
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


def consume_roast_usage(
    session: Session,
    *,
    user_id: str,
    now_ts: float | None,
    cooldown_seconds: int | None,
    max_charges: int | None,
) -> ConsumeRoastResponse:
    """在调用方事务内原子消费普通烧烤充能，不主动 commit。"""

    now = int(float(now_ts or time.time()))
    charge_max, recover_seconds = clamp_charge_settings(max_charges, cooldown_seconds)
    usage = session.execute(select(UserUsage).where(UserUsage.user_id == user_id).with_for_update()).scalar_one_or_none()
    if usage is None:
        charges_left = max(0, charge_max - 1)
        session.add(
            UserUsage(
                user_id=user_id,
                last_roast_ts=now,
                roast_charges=charges_left,
                roast_charge_updated_ts=now,
            )
        )
        return ConsumeRoastResponse(
            allowed=True,
            charges_left=charges_left,
            max_charges=charge_max,
            next_recover_seconds=_next_recover_seconds(charges_left, now, now, charge_max, recover_seconds),
        )

    if usage.roast_charges is None or usage.roast_charge_updated_ts is None:
        charges, updated_ts = _state_from_legacy_last_use(int(usage.last_roast_ts or 0), now, charge_max, recover_seconds)
    else:
        charges, updated_ts = int(usage.roast_charges or 0), int(usage.roast_charge_updated_ts or now)
    charges, updated_ts = _recover_charges(charges, updated_ts, now, charge_max, recover_seconds)
    if charges <= 0:
        remaining = _next_recover_seconds(charges, updated_ts, now, charge_max, recover_seconds)
        usage.roast_charges = charges
        usage.roast_charge_updated_ts = updated_ts
        return ConsumeRoastResponse(
            allowed=False,
            remaining_seconds=remaining,
            charges_left=0,
            max_charges=charge_max,
            next_recover_seconds=remaining,
        )

    was_full = charges >= charge_max
    charges -= 1
    if was_full:
        updated_ts = now
    usage.last_roast_ts = now
    usage.roast_charges = charges
    usage.roast_charge_updated_ts = updated_ts
    return ConsumeRoastResponse(
        allowed=True,
        charges_left=charges,
        max_charges=charge_max,
        next_recover_seconds=_next_recover_seconds(charges, updated_ts, now, charge_max, recover_seconds),
    )


def consume_force_usage(session: Session, *, user_id: str, date_str: dt.date) -> bool:
    """在调用方事务内消费每日加急机会，不主动 commit。"""

    usage = session.execute(select(UserUsage).where(UserUsage.user_id == user_id).with_for_update()).scalar_one_or_none()
    if usage is None:
        session.add(UserUsage(user_id=user_id, last_force_date=date_str))
        return True
    if usage.last_force_date == date_str:
        return False
    usage.last_force_date = date_str
    return True
