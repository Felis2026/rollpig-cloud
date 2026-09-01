from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..config import ROLLPIG_TIMEZONE
from ..db import database_cutoff_value, get_session
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
    """日报投递协调字段使用 UTC naive，不与数据库 server-default 时间混用。"""

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


def _claim_item(row: DailyReportDelivery) -> DailyReportClaimItem:
    """从已持久化的租约生成响应，确保新领取与令牌恢复返回同一口径。"""

    return DailyReportClaimItem(
        date_str=row.date_str,
        group_id=row.group_id,
        delivery_bot_id=row.delivery_bot_id,
        cutoff_at=row.cutoff_at,
        claim_token=str(row.claim_token or ""),
        status=row.status,
        attempt_count=row.attempt_count,
    )


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
            candidate = max(row.cutoff_at, row.next_attempt_at or row.cutoff_at)
        elif (
            row.status == "claimed"
            and row.claim_token not in claimed_tokens
        ):
            candidate = (row.claimed_at or now) + DAILY_REPORT_CLAIM_TIMEOUT
        if candidate is not None and candidate < deadline:
            candidates.append(candidate)
    return min(candidates, default=None)


def _fail_delivery_if_unchanged(
    session: Session,
    row: DailyReportDelivery,
    *,
    reason: str,
) -> bool:
    """仅在租约仍与当前快照一致时标记失败，避免 SQLite 陈旧对象覆盖发送状态。"""

    observed_status = row.status
    observed_claim_token = row.claim_token
    observed_attempt_count = row.attempt_count
    if observed_status not in {"pending", "claimed"}:
        return False
    result = session.execute(
        update(DailyReportDelivery)
        .where(
            DailyReportDelivery.id == row.id,
            DailyReportDelivery.status == observed_status,
            DailyReportDelivery.claim_token == observed_claim_token,
            DailyReportDelivery.attempt_count == observed_attempt_count,
        )
        .values(
            status="failed",
            next_attempt_at=None,
            last_error=row.last_error or reason,
            claim_token=None,
            claimed_at=None,
            instance_id="",
            delivery_bot_id="",
        )
        .execution_options(synchronize_session=False)
    )
    # MySQL 行锁先减少竞争，条件 UPDATE 再为不支持 FOR UPDATE 的 SQLite 兜底。
    session.expire(row)
    session.refresh(row)
    return result.rowcount == 1


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
    cutoff_at = database_cutoff_value(session, req.cutoff_at)

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

    # ================================ 租约恢复与原子领取 ================================ #
    # MySQL 先使用行锁降低竞争；SQLite 会忽略 SELECT FOR UPDATE，因此最终仍以
    # 带旧状态条件的单条 UPDATE 作为跨后端唯一领取依据。
    # sending 之后外部消息结果可能已不可确认，只有到期 pending 或过期 claimed 能重领。
    claimed: list[DailyReportClaimItem] = []
    stale_before = now - DAILY_REPORT_CLAIM_TIMEOUT
    for row in rows:
        deadline = _retry_deadline(row.date_str)
        if now >= deadline and row.status in {"pending", "claimed"}:
            _fail_delivery_if_unchanged(
                session,
                row,
                reason="retry_deadline_exceeded",
            )
            continue
        current_bot_id = candidates[row.group_id][:64]
        same_owner_claim = (
            row.status == "claimed"
            and row.claim_token is not None
            and row.claimed_at is not None
            and row.claimed_at >= stale_before
            and row.instance_id == str(req.instance_id)[:64]
            and row.delivery_bot_id == current_bot_id
        )
        if same_owner_claim:
            # HTTP 响应可能在 Cloud 已提交后丢失；同一投递者可取回原租约，不能再等五分钟。
            claimed.append(_claim_item(row))
            continue
        pending_due = (
            row.status == "pending"
            and now >= row.cutoff_at
            and (row.next_attempt_at is None or row.next_attempt_at <= now)
        )
        stale_claim = (
            row.status == "claimed"
            and (row.claimed_at is None or row.claimed_at < stale_before)
        )
        if not pending_due and not stale_claim:
            continue
        if row.attempt_count >= DAILY_REPORT_MAX_ATTEMPTS:
            _fail_delivery_if_unchanged(
                session,
                row,
                reason="retry_attempts_exhausted",
            )
            continue
        previous_attempt_count = row.attempt_count
        claim_token = uuid.uuid4().hex
        result = session.execute(
            update(DailyReportDelivery)
            .where(
                DailyReportDelivery.id == row.id,
                DailyReportDelivery.attempt_count == previous_attempt_count,
                or_(
                    and_(
                        DailyReportDelivery.status == "pending",
                        DailyReportDelivery.cutoff_at <= now,
                        or_(
                            DailyReportDelivery.next_attempt_at.is_(None),
                            DailyReportDelivery.next_attempt_at <= now,
                        ),
                    ),
                    and_(
                        DailyReportDelivery.status == "claimed",
                        or_(
                            DailyReportDelivery.claimed_at.is_(None),
                            DailyReportDelivery.claimed_at < stale_before,
                        ),
                    ),
                ),
            )
            .values(
                status="claimed",
                instance_id=str(req.instance_id)[:64],
                delivery_bot_id=current_bot_id,
                claim_token=claim_token,
                claimed_at=now,
                attempt_count=previous_attempt_count + 1,
                next_attempt_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        # 另一个 SQLite/MySQL 事务若已先改写状态，条件更新会返回零行；刷新后只报告赢家租约。
        session.expire(row)
        session.refresh(row)
        if result.rowcount != 1:
            continue
        claimed.append(_claim_item(row))
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
    # 截止时间约束的是“是否还允许开始外部发送”。已经进入 sending 的任务可能已经
    # 调用了 Bot API，仍须允许幂等确认及 sent/uncertain 收尾，不能在截止后统一拒绝。
    if (
        req.action == "sending"
        and row.status == "claimed"
        and now >= _retry_deadline(row.date_str)
    ):
        _fail_delivery_if_unchanged(
            session,
            row,
            reason="retry_deadline_exceeded",
        )
        session.commit()
        return DailyReportTransitionResponse(
            ok=False,
            status=row.status,
            attempt_count=row.attempt_count,
            next_attempt_at=row.next_attempt_at,
        )

    allowed_statuses: tuple[str, ...]
    values: dict[str, object] = {
        "last_error": str(req.error or "")[:512],
    }
    response_status = row.status
    response_next_attempt_at = row.next_attempt_at

    # ================================ 投递状态迁移 ================================ #
    if req.action == "sending":
        allowed_statuses = ("claimed", "sending")
        response_status = "sending"
        response_next_attempt_at = None
        values.update(status="sending", next_attempt_at=None)
    elif req.action == "sent":
        allowed_statuses = ("sending", "sent")
        response_status = "sent"
        response_next_attempt_at = None
        values.update(
            status="sent",
            sent_at=func.coalesce(DailyReportDelivery.sent_at, now),
            next_attempt_at=None,
        )
        if req.message_id:
            values["message_id"] = str(req.message_id)[:128]
    elif req.action == "release":
        allowed_statuses = ("claimed",)
        retry_index = max(0, row.attempt_count - 1)
        deadline = _retry_deadline(row.date_str)
        if retry_index >= len(DAILY_REPORT_RETRY_DELAYS):
            response_status = "failed"
            response_next_attempt_at = None
        else:
            next_attempt_at = now + DAILY_REPORT_RETRY_DELAYS[retry_index]
            if next_attempt_at >= deadline:
                response_status = "failed"
                response_next_attempt_at = None
            else:
                response_status = "pending"
                response_next_attempt_at = next_attempt_at
        values.update(
            status=response_status,
            next_attempt_at=response_next_attempt_at,
            claim_token=None,
            claimed_at=None,
            instance_id="",
            delivery_bot_id="",
        )
    elif req.action == "uncertain":
        allowed_statuses = ("sending", "uncertain")
        response_status = "uncertain"
        response_next_attempt_at = None
        values.update(status="uncertain", next_attempt_at=None)
    else:
        allowed_statuses = ("claimed", "skipped")
        response_status = "skipped"
        response_next_attempt_at = None
        values.update(status="skipped", next_attempt_at=None)

    if row.status not in allowed_statuses:
        return DailyReportTransitionResponse(
            ok=False,
            status=row.status,
            attempt_count=row.attempt_count,
            next_attempt_at=row.next_attempt_at,
        )

    result = session.execute(
        update(DailyReportDelivery)
        .where(
            DailyReportDelivery.id == row.id,
            DailyReportDelivery.claim_token == req.claim_token,
            DailyReportDelivery.status.in_(allowed_statuses),
            DailyReportDelivery.attempt_count == row.attempt_count,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        # SQLite 无行锁时可能在读取后发生租约重领；重新读取赢家状态并明确拒绝旧 Token。
        session.rollback()
        current = session.execute(
            select(DailyReportDelivery).where(
                DailyReportDelivery.date_str == req.date_str,
                DailyReportDelivery.group_id == req.group_id,
            )
        ).scalar_one_or_none()
        if current is None:
            return DailyReportTransitionResponse(ok=False)
        return DailyReportTransitionResponse(
            ok=False,
            status=current.status,
            attempt_count=current.attempt_count,
            next_attempt_at=current.next_attempt_at,
        )

    session.commit()
    return DailyReportTransitionResponse(
        ok=True,
        status=response_status,
        attempt_count=row.attempt_count,
        next_attempt_at=response_next_attempt_at,
    )
