from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..config import ROLLPIG_TIMEZONE
from ..db import get_session
from ..models import Collection, DailyReportDelivery, DailyRoll, GroupRoll
from ..schemas import (
    DailyReportClaimItem,
    DailyReportClaimRequest,
    DailyReportClaimResponse,
    DailyReportProfileItem,
    DailyReportProfileRequest,
    DailyReportProfileResponse,
    DailyReportTransitionRequest,
    DailyReportTransitionResponse,
)


router = APIRouter(
    prefix="/v1/daily-reports",
    tags=["daily-reports"],
    dependencies=[Depends(verify_token)],
)
DAILY_REPORT_CLAIM_TIMEOUT = dt.timedelta(minutes=5)
DAILY_REPORT_RETRY_DELAYS = (
    dt.timedelta(seconds=30),
    dt.timedelta(minutes=2),
    dt.timedelta(minutes=5),
)
DAILY_REPORT_MAX_ATTEMPTS = 1 + len(DAILY_REPORT_RETRY_DELAYS)
DAILY_REPORT_RETRY_CUTOFF = dt.time(0, 10)


def _naive_utc(value: dt.datetime) -> dt.datetime:
    """数据库统一存 UTC naive，API 仍接受携带业务时区的截止时间。"""

    if value.tzinfo is None:
        return value
    return value.astimezone(dt.timezone.utc).replace(tzinfo=None)


def _utc_now() -> dt.datetime:
    """集中当前时间入口，保证领取和重试边界可稳定测试。"""

    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def _retry_deadline(date_str: dt.date) -> dt.datetime:
    """日报只允许在业务日期次日 00:10 前继续安全重试。"""

    local_deadline = dt.datetime.combine(
        date_str + dt.timedelta(days=1),
        DAILY_REPORT_RETRY_CUTOFF,
        tzinfo=ROLLPIG_TIMEZONE,
    )
    return _naive_utc(local_deadline)


def _expert_level_from_copies(copies: int | None) -> int | None:
    """历史抽取缺少 copies 快照时返回 None，由客户端隐藏该排行项。"""

    if copies is None:
        return None
    return min(max(int(copies) - 1, 0), 5)


def _clear_claim(row: DailyReportDelivery) -> None:
    """释放领取凭证；终态和待重试状态都不能继续复用旧令牌。"""

    row.claim_token = None
    row.claimed_at = None
    row.instance_id = ""
    row.delivery_bot_id = ""


def _user_id_batches(user_ids: tuple[str, ...], size: int = 500) -> tuple[tuple[str, ...], ...]:
    """按数据库通用参数预算拆批；接口仍是一次群级请求，不退化为逐用户访问。"""

    return tuple(
        user_ids[index : index + size]
        for index in range(0, len(user_ids), size)
    )


def _next_claim_at(
    rows: list[DailyReportDelivery],
    claimed_tokens: set[str],
    now: dt.datetime,
) -> dt.datetime | None:
    """返回客户端下一次值得领取的时刻，覆盖服务端退避和其他实例租约过期。"""

    candidates: list[dt.datetime] = []
    for row in rows:
        deadline = _retry_deadline(row.date_str)
        candidate: dt.datetime | None = None
        if row.status == "pending":
            candidate = row.next_attempt_at or now
        elif (
            row.status == "claimed"
            and row.claim_token not in claimed_tokens
        ):
            candidate = (row.claimed_at or now) + DAILY_REPORT_CLAIM_TIMEOUT
        if candidate is not None and candidate < deadline:
            candidates.append(candidate)
    return min(candidates, default=None)


# ================================ 群级排行资料 ================================ #


@router.post("/profiles", response_model=DailyReportProfileResponse)
def get_daily_report_profiles(
    req: DailyReportProfileRequest,
    session: Session = Depends(get_session),
) -> DailyReportProfileResponse:
    """批量返回固定截止点下的 EX、图鉴数量和可用小猪引用。"""

    user_ids = tuple(req.user_ids)
    if not user_ids:
        return DailyReportProfileResponse()
    cutoff_at = _naive_utc(req.cutoff_at)

    group_rolls: dict[str, str] = {}
    daily_rolls: dict[str, DailyRoll] = {}
    collection_stats: dict[str, tuple[int, dt.datetime | None]] = {}
    recent_rolls: dict[str, DailyRoll] = {}
    for batch in _user_id_batches(user_ids):
        # ================================ 当日群抽取与成长 ================================ #
        group_rolls.update({
            row.user_id: row.pig_id
            for row in session.execute(
                select(GroupRoll).where(
                    GroupRoll.group_id == req.group_id,
                    GroupRoll.date_str == req.date_str,
                    GroupRoll.user_id.in_(batch),
                    GroupRoll.seen_at <= cutoff_at,
                )
            ).scalars()
        })
        daily_rolls.update({
            row.user_id: row
            for row in session.execute(
                select(DailyRoll).where(
                    DailyRoll.date_str == req.date_str,
                    DailyRoll.user_id.in_(batch),
                    DailyRoll.created_at <= cutoff_at,
                )
            ).scalars()
        })

        # ================================ 图鉴数量与最近小猪 ================================ #
        collection_stats.update({
            str(user_id): (int(count or 0), achieved_at)
            for user_id, count, achieved_at in session.execute(
                select(
                    Collection.user_id,
                    func.count(Collection.id),
                    func.max(Collection.first_seen_at),
                )
                .where(
                    Collection.user_id.in_(batch),
                    Collection.first_seen_at <= cutoff_at,
                )
                .group_by(Collection.user_id)
            )
        })
        latest_dates = (
            select(
                DailyRoll.user_id.label("user_id"),
                func.max(DailyRoll.date_str).label("date_str"),
            )
            .where(
                DailyRoll.user_id.in_(batch),
                DailyRoll.date_str <= req.date_str,
                DailyRoll.created_at <= cutoff_at,
            )
            .group_by(DailyRoll.user_id)
            .subquery()
        )
        recent_rolls.update({
            row.user_id: row
            for row in session.execute(
                select(DailyRoll).join(
                    latest_dates,
                    and_(
                        DailyRoll.user_id == latest_dates.c.user_id,
                        DailyRoll.date_str == latest_dates.c.date_str,
                    ),
                )
            ).scalars()
        })

    # ================================ 稳定响应组装 ================================ #
    items: list[DailyReportProfileItem] = []
    for user_id in user_ids:
        daily_pig_id = str(group_rolls.get(user_id) or "")
        daily_roll = daily_rolls.get(user_id)
        daily_roll_matches = bool(
            daily_roll is not None and daily_roll.pig_id == daily_pig_id
        )
        catalog_count, catalog_achieved_at = collection_stats.get(user_id, (0, None))
        recent_roll = recent_rolls.get(user_id)
        items.append(
            DailyReportProfileItem(
                user_id=user_id,
                daily_pig_id=daily_pig_id,
                daily_ex_level=(
                    _expert_level_from_copies(daily_roll.copies_after_roll)
                    if daily_roll_matches
                    else None
                ),
                daily_achieved_at=(daily_roll.created_at if daily_roll_matches else None),
                catalog_count=catalog_count,
                catalog_achieved_at=catalog_achieved_at,
                recent_pig_id=(recent_roll.pig_id if recent_roll is not None else ""),
                recent_ex_level=(
                    _expert_level_from_copies(recent_roll.copies_after_roll)
                    if recent_roll is not None
                    else None
                ),
            )
        )
    return DailyReportProfileResponse(items=items)


# ================================ 群日唯一领取 ================================ #


def _claim_once(
    session: Session,
    req: DailyReportClaimRequest,
) -> DailyReportClaimResponse:
    """在一个事务内创建日期群唯一行，并领取未发送或租约过期的任务。"""

    # ================================ 候选群归一化 ================================ #
    candidates = {
        str(item.group_id): str(item.delivery_bot_id)
        for item in req.candidates
        if item.group_id and item.delivery_bot_id
    }
    if not candidates:
        return DailyReportClaimResponse()

    now = _utc_now()
    cutoff_at = _naive_utc(req.cutoff_at)

    # ================================ 日期群唯一记录 ================================ #
    # 新日期群先落 pending 行；并发插入由唯一键和外层 IntegrityError 重试裁决。
    existing_groups = set(
        session.execute(
            select(DailyReportDelivery.group_id).where(
                DailyReportDelivery.date_str == req.date_str,
                DailyReportDelivery.group_id.in_(candidates),
            )
        ).scalars()
    )
    for group_id in sorted(set(candidates) - existing_groups):
        session.add(
            DailyReportDelivery(
                date_str=req.date_str,
                group_id=group_id,
                cutoff_at=cutoff_at,
            )
        )
    session.flush()

    rows = session.execute(
        select(DailyReportDelivery)
        .where(
            DailyReportDelivery.date_str == req.date_str,
            DailyReportDelivery.group_id.in_(candidates),
        )
        .order_by(DailyReportDelivery.group_id)
        .with_for_update()
    ).scalars().all()

    # ================================ 租约领取 ================================ #
    # sending 之后外部消息结果可能已不可确认，只有到期 pending 或过期 claimed 能重领。
    claimed: list[DailyReportClaimItem] = []
    stale_before = now - DAILY_REPORT_CLAIM_TIMEOUT
    for row in rows:
        deadline = _retry_deadline(row.date_str)
        if now >= deadline and row.status in {"pending", "claimed"}:
            row.status = "failed"
            row.next_attempt_at = None
            row.last_error = row.last_error or "retry_deadline_exceeded"
            _clear_claim(row)
            continue
        pending_due = (
            row.status == "pending"
            and (row.next_attempt_at is None or row.next_attempt_at <= now)
        )
        reclaimable = pending_due or (
            row.status == "claimed"
            and (row.claimed_at is None or row.claimed_at < stale_before)
        )
        if not reclaimable:
            continue
        if row.attempt_count >= DAILY_REPORT_MAX_ATTEMPTS:
            row.status = "failed"
            row.next_attempt_at = None
            row.last_error = row.last_error or "retry_attempts_exhausted"
            _clear_claim(row)
            continue
        row.status = "claimed"
        row.instance_id = str(req.instance_id)[:64]
        row.delivery_bot_id = candidates[row.group_id][:64]
        row.claim_token = uuid.uuid4().hex
        row.claimed_at = now
        row.attempt_count += 1
        row.next_attempt_at = None
        claimed.append(
            DailyReportClaimItem(
                date_str=row.date_str,
                group_id=row.group_id,
                delivery_bot_id=row.delivery_bot_id,
                cutoff_at=row.cutoff_at,
                claim_token=row.claim_token,
                status=row.status,
                attempt_count=row.attempt_count,
            )
        )
    next_claim_at = _next_claim_at(
        rows,
        {item.claim_token for item in claimed},
        now,
    )
    session.commit()
    return DailyReportClaimResponse(
        items=claimed,
        next_claim_at=next_claim_at,
    )


@router.post("/claim", response_model=DailyReportClaimResponse)
def claim_daily_reports(
    req: DailyReportClaimRequest,
    session: Session = Depends(get_session),
):
    # 无行可锁时由日期群唯一键裁决；并发插入冲突后回滚并重查赢家创建的记录。
    for _ in range(3):
        try:
            return _claim_once(session, req)
        except IntegrityError:
            session.rollback()
    raise RuntimeError("claim daily reports retry exhausted")


@router.post("/transition", response_model=DailyReportTransitionResponse)
def transition_daily_report(
    req: DailyReportTransitionRequest,
    session: Session = Depends(get_session),
):
    """推进发送边界；一旦进入 sending，租约失效也绝不能自动重领。"""

    row = session.execute(
        select(DailyReportDelivery)
        .where(
            DailyReportDelivery.date_str == req.date_str,
            DailyReportDelivery.group_id == req.group_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None or row.claim_token != req.claim_token:
        return DailyReportTransitionResponse(ok=False)

    now = _utc_now()

    # ================================ 投递状态迁移 ================================ #
    if req.action == "sending":
        if row.status not in {"claimed", "sending"}:
            return DailyReportTransitionResponse(
                ok=False,
                status=row.status,
                attempt_count=row.attempt_count,
                next_attempt_at=row.next_attempt_at,
            )
        row.status = "sending"
        row.next_attempt_at = None
    elif req.action == "sent":
        if row.status not in {"sending", "sent"}:
            return DailyReportTransitionResponse(
                ok=False,
                status=row.status,
                attempt_count=row.attempt_count,
                next_attempt_at=row.next_attempt_at,
            )
        row.status = "sent"
        row.sent_at = row.sent_at or now
        row.next_attempt_at = None
        if req.message_id:
            row.message_id = str(req.message_id)[:128]
    elif req.action == "release":
        if row.status != "claimed":
            return DailyReportTransitionResponse(
                ok=False,
                status=row.status,
                attempt_count=row.attempt_count,
                next_attempt_at=row.next_attempt_at,
            )
        retry_index = max(0, row.attempt_count - 1)
        deadline = _retry_deadline(row.date_str)
        if retry_index >= len(DAILY_REPORT_RETRY_DELAYS):
            row.status = "failed"
            row.next_attempt_at = None
        else:
            next_attempt_at = now + DAILY_REPORT_RETRY_DELAYS[retry_index]
            if next_attempt_at >= deadline:
                row.status = "failed"
                row.next_attempt_at = None
            else:
                row.status = "pending"
                row.next_attempt_at = next_attempt_at
        _clear_claim(row)
    elif req.action == "uncertain":
        if row.status not in {"sending", "uncertain"}:
            return DailyReportTransitionResponse(
                ok=False,
                status=row.status,
                attempt_count=row.attempt_count,
                next_attempt_at=row.next_attempt_at,
            )
        row.status = "uncertain"
        row.next_attempt_at = None
    else:
        if row.status not in {"claimed", "skipped"}:
            return DailyReportTransitionResponse(
                ok=False,
                status=row.status,
                attempt_count=row.attempt_count,
                next_attempt_at=row.next_attempt_at,
            )
        row.status = "skipped"
        row.next_attempt_at = None

    row.last_error = str(req.error or "")[:512]
    session.commit()
    return DailyReportTransitionResponse(
        ok=True,
        status=row.status,
        attempt_count=row.attempt_count,
        next_attempt_at=row.next_attempt_at,
    )
