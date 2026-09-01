from __future__ import annotations

import datetime as dt
import time
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import database_cutoff_value
from ..models import GroupDailyActiveUser, GroupRoastRefillRequest, UserUsage
from ..schemas import (
    GroupRoastRefillCompleteResponse,
    GroupRoastRefillItem,
    GroupRoastRefillPrepareRequest,
    GroupRoastRefillPrepareResponse,
)
from .usage import clamp_charge_settings


ROAST_REFILL_TTL_SECONDS = 10 * 60
ROAST_REFILL_THRESHOLD_POLICY = "capped-v1"
ROAST_REFILL_THRESHOLD_STEPS = ((25, 8), (35, 12), (45, 16), (55, 20))
LEGACY_ROAST_REFILL_RATIOS = (25, 35, 45, 55, 65)


def _utc_from_ts(value: float | None = None) -> dt.datetime:
    aware = (
        dt.datetime.fromtimestamp(float(value), dt.timezone.utc)
        if value is not None
        else dt.datetime.now(dt.timezone.utc)
    )
    # 补货生命周期由应用主动写入 UTC-naive；server-default 活动时间仍沿用数据库时区。
    return aware.replace(tzinfo=None)


def _active_key(date_str: dt.date, group_id: str) -> str:
    return f"{date_str.isoformat()}:{group_id}"


def refill_threshold(active_count: int, success_count: int) -> tuple[int, int]:
    """返回封顶后的本轮门槛；第四次起固定为 55% / 20 票。"""

    normalized_active = max(0, int(active_count or 0))
    normalized_success = max(0, int(success_count or 0))
    ratio, vote_cap = ROAST_REFILL_THRESHOLD_STEPS[
        min(normalized_success, len(ROAST_REFILL_THRESHOLD_STEPS) - 1)
    ]
    proportional_votes = (normalized_active * ratio + 99) // 100
    return ratio, max(2, min(proportional_votes, vote_cap))


def legacy_refill_threshold(active_count: int, success_count: int) -> tuple[int, int]:
    """保留旧 Plus 使用的五档无上限算法，供 Cloud 优先滚动升级。"""

    normalized_active = max(0, int(active_count or 0))
    normalized_success = max(0, int(success_count or 0))
    ratio = LEGACY_ROAST_REFILL_RATIOS[
        min(normalized_success, len(LEGACY_ROAST_REFILL_RATIOS) - 1)
    ]
    return ratio, max(2, (normalized_active * ratio + 99) // 100)


def refill_to_schema(row: GroupRoastRefillRequest) -> GroupRoastRefillItem:
    return GroupRoastRefillItem(
        request_id=row.request_id,
        date_str=row.date_str,
        group_id=row.group_id,
        initiator_id=row.initiator_id,
        initiator_name=row.initiator_name,
        delivery_bot_id=row.delivery_bot_id,
        message_id=row.message_id,
        active_count_snapshot=row.active_count_snapshot,
        required_ratio=row.required_ratio,
        required_votes=row.required_votes,
        success_count_before=row.success_count_before,
        status=row.status,
        created_at=row.created_at,
        expires_at=row.expires_at,
        completed_at=row.completed_at,
        benefited_user_ids=sorted({str(user_id) for user_id in (row.benefited_user_ids or []) if user_id}),
        failure_reason=row.failure_reason,
    )


# ================================ 群日活维护 ================================ #

def mark_group_active_users(
    session: Session,
    *,
    date_str: dt.date,
    group_id: str,
    user_ids: list[str],
) -> None:
    """幂等写入群日活；MySQL/SQLite 使用单条批量 upsert 压低高频路径开销。"""

    normalized = sorted({str(user_id) for user_id in user_ids if user_id})
    if not group_id or not normalized:
        return
    values = [
        {
            "date_str": date_str,
            "group_id": str(group_id),
            "user_id": user_id,
        }
        for user_id in normalized
    ]
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "mysql":
        statement = mysql_insert(GroupDailyActiveUser).values(values)
        session.execute(statement.on_duplicate_key_update(user_id=statement.inserted.user_id))
        return
    if dialect_name == "sqlite":
        statement = sqlite_insert(GroupDailyActiveUser).values(values)
        session.execute(statement.on_conflict_do_nothing(
            index_elements=[
                GroupDailyActiveUser.date_str,
                GroupDailyActiveUser.group_id,
                GroupDailyActiveUser.user_id,
            ]
        ))
        return

    # 未知数据库保留可移植 fallback；savepoint 隔离并发唯一键冲突。
    existing = set(
        session.execute(
            select(GroupDailyActiveUser.user_id).where(
                GroupDailyActiveUser.date_str == date_str,
                GroupDailyActiveUser.group_id == str(group_id),
                GroupDailyActiveUser.user_id.in_(normalized),
            )
        ).scalars()
    )
    for user_id in normalized:
        if user_id in existing:
            continue
        try:
            with session.begin_nested():
                session.add(GroupDailyActiveUser(date_str=date_str, group_id=str(group_id), user_id=user_id))
                session.flush()
        except IntegrityError:
            # 另一实例已先写入同一唯一键时，本次幂等登记仍视为成功。
            continue


def get_group_active_user_ids(
    session: Session,
    date_str: dt.date,
    group_id: str,
    cutoff_at: dt.datetime | None = None,
) -> list[str]:
    stmt = select(GroupDailyActiveUser.user_id).where(
        GroupDailyActiveUser.date_str == date_str,
        GroupDailyActiveUser.group_id == str(group_id),
    )
    if cutoff_at is not None:
        normalized_cutoff = database_cutoff_value(session, cutoff_at)
        stmt = stmt.where(GroupDailyActiveUser.active_at <= normalized_cutoff)
    return sorted(session.execute(stmt).scalars())


def _reset_roast_charges(
    session: Session,
    *,
    user_ids: list[str],
    charge_max: int,
    now_epoch: int,
) -> None:
    """批量满格普通配额；MySQL/SQLite 使用 upsert 避免跨群并发创建同一 usage 行冲突。"""

    if not user_ids:
        return
    values = [
        {
            "user_id": user_id,
            "roast_charges": charge_max,
            "roast_charge_updated_ts": now_epoch,
        }
        for user_id in user_ids
    ]
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "mysql":
        statement = mysql_insert(UserUsage).values(values)
        session.execute(statement.on_duplicate_key_update(
            roast_charges=statement.inserted.roast_charges,
            roast_charge_updated_ts=statement.inserted.roast_charge_updated_ts,
        ))
        return
    if dialect_name == "sqlite":
        statement = sqlite_insert(UserUsage).values(values)
        session.execute(statement.on_conflict_do_update(
            index_elements=[UserUsage.user_id],
            set_={
                "roast_charges": statement.excluded.roast_charges,
                "roast_charge_updated_ts": statement.excluded.roast_charge_updated_ts,
            },
        ))
        return

    # 未知数据库仍保留可移植实现；行锁保证已有记录的更新顺序稳定。
    usage_rows = {
        usage.user_id: usage
        for usage in session.execute(
            select(UserUsage).where(UserUsage.user_id.in_(user_ids)).with_for_update()
        ).scalars()
    }
    for user_id in user_ids:
        usage = usage_rows.get(user_id)
        if usage is None:
            usage = UserUsage(user_id=user_id)
            session.add(usage)
        usage.roast_charges = charge_max
        usage.roast_charge_updated_ts = now_epoch


# ================================ 申请生命周期 ================================ #

def _expire_if_needed(row: GroupRoastRefillRequest, now: dt.datetime) -> bool:
    if row.status != "voting" or row.expires_at > now:
        return False
    row.status = "expired"
    row.failure_reason = "expired"
    row.active_key = None
    return True


def prepare_refill(session: Session, req: GroupRoastRefillPrepareRequest) -> GroupRoastRefillPrepareResponse:
    """冻结门槛并创建申请；active_key 唯一键负责兜底无行可锁时的并发竞争。"""

    now = _utc_from_ts(req.now_ts)
    active_key = _active_key(req.date_str, req.group_id)
    existing = session.execute(
        select(GroupRoastRefillRequest)
        .where(GroupRoastRefillRequest.active_key == active_key)
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        if _expire_if_needed(existing, now):
            session.flush()
        if existing.status == "voting":
            return GroupRoastRefillPrepareResponse(status="existing", request=refill_to_schema(existing))

    active_user_ids = get_group_active_user_ids(session, req.date_str, req.group_id)
    if len(active_user_ids) < 3:
        return GroupRoastRefillPrepareResponse(
            status="insufficient_active",
            active_user_ids=active_user_ids,
        )

    success_count = int(
        session.execute(
            select(func.count(GroupRoastRefillRequest.id)).where(
                GroupRoastRefillRequest.date_str == req.date_str,
                GroupRoastRefillRequest.group_id == str(req.group_id),
                GroupRoastRefillRequest.status == "succeeded",
            )
        ).scalar_one()
    )
    # 旧 Plus 不会声明门槛能力，继续使用原算法；新 Plus 显式协商后才启用
    # 封顶门槛，确保 Cloud 可以先部署而不改变旧客户端行为。
    threshold = (
        refill_threshold
        if req.threshold_policy == ROAST_REFILL_THRESHOLD_POLICY
        else legacy_refill_threshold
    )
    ratio, required_votes = threshold(len(active_user_ids), success_count)
    row = GroupRoastRefillRequest(
        request_id=uuid.uuid4().hex,
        active_key=active_key,
        date_str=req.date_str,
        group_id=str(req.group_id),
        initiator_id=str(req.initiator_id),
        initiator_name=str(req.initiator_name),
        delivery_bot_id=str(req.delivery_bot_id),
        message_id="",
        active_count_snapshot=len(active_user_ids),
        required_ratio=ratio,
        required_votes=required_votes,
        success_count_before=success_count,
        status="voting",
        benefited_user_ids=[],
        failure_reason="",
        created_at=now,
        expires_at=now + dt.timedelta(seconds=ROAST_REFILL_TTL_SECONDS),
    )
    session.add(row)
    session.flush()
    return GroupRoastRefillPrepareResponse(
        status="created",
        request=refill_to_schema(row),
        active_user_ids=active_user_ids,
    )


def get_active_refill(
    session: Session,
    *,
    date_str: dt.date,
    group_id: str,
    now_ts: float | None = None,
) -> GroupRoastRefillRequest | None:
    row = session.execute(
        select(GroupRoastRefillRequest)
        .where(GroupRoastRefillRequest.active_key == _active_key(date_str, group_id))
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        return None
    if _expire_if_needed(row, _utc_from_ts(now_ts)):
        return None
    return row if row.status == "voting" else None


def bind_refill_message(
    session: Session,
    *,
    request_id: str,
    message_id: str,
    now_ts: float | None = None,
) -> GroupRoastRefillRequest | None:
    row = session.execute(
        select(GroupRoastRefillRequest)
        .where(GroupRoastRefillRequest.request_id == str(request_id))
        .with_for_update()
    ).scalar_one_or_none()
    if row is None or row.status != "voting":
        return None
    # 发送投票消息可能卡住到 TTL 之后；必须在锁内先过期，不能绑定一场已失效投票。
    if _expire_if_needed(row, _utc_from_ts(now_ts)):
        return None
    if not message_id or (row.message_id and row.message_id != str(message_id)):
        return None
    row.message_id = str(message_id)
    session.flush()
    return row


def fail_refill(
    session: Session,
    *,
    request_id: str,
    message_id: str,
    reason: str,
) -> bool:
    row = session.execute(
        select(GroupRoastRefillRequest)
        .where(GroupRoastRefillRequest.request_id == str(request_id))
        .with_for_update()
    ).scalar_one_or_none()
    if row is None or row.status != "voting":
        return False
    if message_id and row.message_id not in {"", str(message_id)}:
        return False
    row.status = "failed"
    row.failure_reason = str(reason)[:64]
    row.active_key = None
    return True


# ================================ 原子验票与满格 ================================ #

def complete_refill(
    session: Session,
    *,
    request_id: str,
    message_id: str,
    voter_ids: list[str],
    excluded_user_ids: list[str],
    max_charges: int = 2,
    now_ts: float | None = None,
) -> GroupRoastRefillCompleteResponse:
    """锁定申请后重新按最新日活验票，并在同一事务中批量重置全局普通配额。"""

    now = _utc_from_ts(now_ts)
    row = session.execute(
        select(GroupRoastRefillRequest)
        .where(GroupRoastRefillRequest.request_id == str(request_id))
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        return GroupRoastRefillCompleteResponse(completed=False, status="missing")
    if row.status != "voting":
        return GroupRoastRefillCompleteResponse(
            completed=False,
            status=row.status,
            request=refill_to_schema(row),
        )
    if not row.message_id or not message_id or row.message_id != str(message_id):
        return GroupRoastRefillCompleteResponse(
            completed=False,
            status="message_mismatch",
            request=refill_to_schema(row),
        )
    if _expire_if_needed(row, now):
        return GroupRoastRefillCompleteResponse(
            completed=False,
            status="expired",
            request=refill_to_schema(row),
        )

    active_user_ids = set(get_group_active_user_ids(session, row.date_str, row.group_id))
    excluded = {str(user_id) for user_id in excluded_user_ids if user_id}
    valid_voters = sorted(({str(user_id) for user_id in voter_ids if user_id} & active_user_ids) - excluded)
    if len(valid_voters) < row.required_votes:
        return GroupRoastRefillCompleteResponse(
            completed=False,
            status="pending",
            request=refill_to_schema(row),
            valid_voter_ids=valid_voters,
        )

    benefited = sorted(active_user_ids - excluded)
    now_epoch = int(float(now_ts)) if now_ts is not None else int(time.time())
    charge_max, _ = clamp_charge_settings(max_charges, None)
    _reset_roast_charges(
        session,
        user_ids=benefited,
        charge_max=charge_max,
        now_epoch=now_epoch,
    )

    row.status = "succeeded"
    row.active_key = None
    row.completed_at = now
    row.benefited_user_ids = benefited
    session.flush()
    return GroupRoastRefillCompleteResponse(
        completed=True,
        status="succeeded",
        request=refill_to_schema(row),
        valid_voter_ids=valid_voters,
        benefited_user_ids=benefited,
    )
