from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session, run_transaction_with_sqlite_lock_retry
from ..models import DailyRoll, RoastReservation, RoastReservationMessage, UnrolledRoastAttempt
from ..schemas import (
    RoastReservationClaimRequest,
    RoastReservationClaimResponse,
    RoastReservationCompleteRequest,
    RoastReservationMutationRequest,
    RoastReservationMutationResponse,
    RoastReservationOutcomeRequest,
    RoastReservationOwnedResponse,
    RoastReservationPrepareRequest,
    RoastReservationPrepareResponse,
    RoastReservationBindMessageRequest,
    RoastReservationReplyJoinRequest,
    UnrolledRoastAttemptRequest,
    UnrolledRoastAttemptResponse,
)
from ..services.events import bind_reservation_event, record_roast_event
from ..services.progress import apply_daily_feed
from ..services.reservations import activate_if_target_already_rolled, prepare_reservation, reservation_to_schema


router = APIRouter(
    prefix="/v1/roast-reservations",
    tags=["roast-reservations"],
    dependencies=[Depends(verify_token)],
)
ROAST_RESERVATION_CLAIM_TIMEOUT = dt.timedelta(minutes=5)


# ================================ 回复消息加入预约 ================================ #


@router.post("/bind-message")
def bind_message(req: RoastReservationBindMessageRequest, session: Session = Depends(get_session)):
    """持久化通知关联；重试幂等，同一消息不可指向另一场预约。"""

    def transaction():
        reservation = session.scalar(select(RoastReservation).where(
            RoastReservation.reservation_id == req.reservation_id,
            RoastReservation.group_id == req.group_id,
            RoastReservation.date_str == req.date_str,
        ))
        if reservation is None:
            return {"ok": False}
        existing = session.scalar(select(RoastReservationMessage).where(
            RoastReservationMessage.bot_id == req.bot_id,
            RoastReservationMessage.group_id == req.group_id,
            RoastReservationMessage.message_id == req.message_id,
        ))
        if existing is not None:
            return {"ok": existing.reservation_id == req.reservation_id}
        # 留七天用于过期回复识别，不永久积累通知关联。
        session.execute(delete(RoastReservationMessage).where(
            RoastReservationMessage.date_str < req.date_str - dt.timedelta(days=7)
        ))
        session.add(RoastReservationMessage(**req.model_dump()))
        session.commit()
        return {"ok": True}

    for attempt in range(2):
        try:
            return run_transaction_with_sqlite_lock_retry(session, transaction)
        except IntegrityError:
            session.rollback()
            if attempt:
                raise


@router.post("/join-by-message", response_model=RoastReservationPrepareResponse)
def join_by_message(req: RoastReservationReplyJoinRequest, session: Session = Depends(get_session)):
    """按通知加入既有预约，用户今日小猪由账本读取，不信任客户端伪造。"""

    def transaction():
        binding = session.scalar(select(RoastReservationMessage).where(
            RoastReservationMessage.bot_id == req.bot_id,
            RoastReservationMessage.group_id == req.group_id,
            RoastReservationMessage.message_id == req.message_id,
        ))
        if binding is None:
            return RoastReservationPrepareResponse(status="message_not_found")
        reservation = session.scalar(select(RoastReservation).where(
            RoastReservation.reservation_id == binding.reservation_id,
            RoastReservation.group_id == req.group_id,
            RoastReservation.date_str == req.date_str,
        ).with_for_update())
        if reservation is None or reservation.status != "pending":
            return RoastReservationPrepareResponse(status="reservation_closed")
        if reservation.target_id == req.attacker_id:
            return RoastReservationPrepareResponse(status="self_target")
        roll = session.scalar(select(DailyRoll).where(
            DailyRoll.user_id == req.attacker_id,
            DailyRoll.date_str == req.date_str,
        ))
        if roll is None or (roll.appearance_snapshot or {}).get("is_makeup") is True:
            return RoastReservationPrepareResponse(status="attacker_unrolled")
        response = prepare_reservation(session, RoastReservationPrepareRequest(
            attacker_id=req.attacker_id, attacker_name=req.attacker_name,
            attacker_pig_id=roll.pig_id, target_id=reservation.target_id,
            target_name=reservation.target_name, group_id=req.group_id,
            delivery_bot_id=req.bot_id, date_str=req.date_str,
        ), expected_reservation_id=reservation.reservation_id)
        session.commit()
        return response

    return run_transaction_with_sqlite_lock_retry(session, transaction)


@router.post("/unrolled-attempt", response_model=UnrolledRoastAttemptResponse)
def record_unrolled_attempt(req: UnrolledRoastAttemptRequest, session: Session = Depends(get_session)):
    for _ in range(2):
        row = session.execute(
            select(UnrolledRoastAttempt)
            .where(UnrolledRoastAttempt.date_str == req.date_str, UnrolledRoastAttempt.user_id == req.user_id)
            .with_for_update()
        ).scalar_one_or_none()
        try:
            if row is None:
                row = UnrolledRoastAttempt(date_str=req.date_str, user_id=req.user_id, attempt_count=1)
                session.add(row)
            else:
                row.attempt_count = int(row.attempt_count or 0) + 1
            session.commit()
            return UnrolledRoastAttemptResponse(user_id=req.user_id, date_str=req.date_str, count=row.attempt_count)
        except IntegrityError:
            session.rollback()
    raise RuntimeError("unrolled roast attempt retry exhausted")


@router.post("/prepare", response_model=RoastReservationPrepareResponse)
def prepare(req: RoastReservationPrepareRequest, session: Session = Depends(get_session)):
    # 唯一键冲突时整个事务（包括充能消费）一起回滚，再按“加入已有预约”重试。
    for _ in range(2):
        try:
            response = prepare_reservation(session, req)
            session.commit()
            if response.status in {"reservation_created", "reservation_joined", "already_joined"}:
                # 新事务读取最新 DailyRoll，和 daily-roll 创建事务内的激活形成双保险。
                # 两边都幂等，因此无论谁先提交都不会留下永久 pending。
                if activate_if_target_already_rolled(
                    session,
                    date_str=req.date_str,
                    target_id=req.target_id,
                ):
                    session.commit()
            return response
        except IntegrityError:
            session.rollback()
    raise RuntimeError("prepare roast reservation retry exhausted")


@router.get("/owned", response_model=RoastReservationOwnedResponse)
def owned(delivery_bot_id: str, date_str: dt.date, session: Session = Depends(get_session)):
    exists = session.execute(
        select(RoastReservation.id).where(
            RoastReservation.delivery_bot_id == delivery_bot_id,
            RoastReservation.date_str == date_str,
            RoastReservation.status.in_(("pending", "ready", "processing", "prepared")),
        ).limit(1)
    ).first()
    return RoastReservationOwnedResponse(has_owned=exists is not None)


@router.post("/claim", response_model=RoastReservationClaimResponse)
def claim(req: RoastReservationClaimRequest, session: Session = Depends(get_session)):
    # ================================ 兼容领取与本地退避 ================================ #
    # 新 Plus 可以安全处理 prepared；旧 Plus 缺少 capability，只能收到它认识的
    # processing/sending。排除列表只用于跳过本机暂缓项，不改变 Cloud 持久状态。
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    stale_before = now - ROAST_RESERVATION_CLAIM_TIMEOUT
    excluded_ids = {
        str(reservation_id)
        for reservation_id in req.excluded_reservation_ids[:64]
        if reservation_id
    }
    claim_query = (
        select(RoastReservation)
        .where(
            RoastReservation.delivery_bot_id == req.delivery_bot_id,
            RoastReservation.date_str == req.date_str,
            (
                (RoastReservation.status == "ready")
                | (
                    RoastReservation.status.in_(("processing", "prepared"))
                    # processing 与 prepared 都没有调用外部发送接口，可以安全回收；
                    # 旧版 processing + snapshot 已在启动迁移中冻结为 sending。
                    & (
                        RoastReservation.claimed_at.is_(None)
                        | (RoastReservation.claimed_at < stale_before)
                    )
                )
            ),
        )
        .order_by(RoastReservation.id)
        .limit(max(1, min(12, req.limit)))
        .with_for_update()
    )
    if excluded_ids:
        claim_query = claim_query.where(~RoastReservation.reservation_id.in_(excluded_ids))
    rows = session.execute(claim_query).scalars().all()
    for row in rows:
        if row.outcome_snapshot is not None:
            # 旧客户端保存 outcome 后就直接发送；将固定快照交付为 sending，避免它
            # 在 prepared -> 旧 /outcome -> release 之间永久循环。
            row.status = "prepared" if req.supports_prepared else "sending"
        else:
            row.status = "processing"
        row.claim_token = uuid.uuid4().hex
        row.claimed_at = now
    session.flush()

    # ================================ 单请求 Owner 状态 ================================ #
    # claim 同时返回 has_owned，使 Owner-only 轮询无需再追加 /owned 请求。被本地
    # 排除的预约仍计入 owned，避免暂缓期间错误停止后续恢复。
    items = [reservation_to_schema(session, row) for row in rows]
    has_owned = session.execute(
        select(RoastReservation.id).where(
            RoastReservation.delivery_bot_id == req.delivery_bot_id,
            RoastReservation.date_str == req.date_str,
            RoastReservation.status.in_(("pending", "ready", "processing", "prepared")),
        ).limit(1)
    ).first() is not None
    session.commit()
    return RoastReservationClaimResponse(items=items, has_owned=has_owned)


def _prepare_outcome_once(
    req: RoastReservationOutcomeRequest,
    session: Session,
) -> RoastReservationMutationResponse:
    """幂等固化结果；相同 token 只能绑定同一份 outcome snapshot。"""

    row = session.execute(
        select(RoastReservation).where(RoastReservation.reservation_id == req.reservation_id).with_for_update()
    ).scalar_one_or_none()
    if (
        row is None
        or row.claim_token != req.claim_token
        or row.status not in {"processing", "prepared", "sending", "completed"}
    ):
        return RoastReservationMutationResponse(ok=False)
    if row.outcome_snapshot is None:
        if row.status != "processing":
            return RoastReservationMutationResponse(ok=False)
        row.outcome_snapshot = req.outcome_snapshot
        # ================================ 预约加餐结算 ================================ #
        # 只有新客户端明确声明、普通预约且结果成功时结算。结果与预约快照在同一
        # 事务中固化，重领时只读取已保存结果，不会重复成长或改变群内提示。
        if (
            req.settle_daily_feed
            and row.force_mode not in {"normal", "super"}
            and str(req.outcome_snapshot.get("event_type") or "") == "success"
        ):
            reservation_item = reservation_to_schema(session, row)
            # 多场预约可能共享参与者；统一按 user_id 获取用户行锁，避免两场事务
            # 以相反参与顺序结算时形成死锁。最终结果仍按原参与顺序固化和展示。
            feed_results_by_user = {
                participant.user_id: apply_daily_feed(
                    session,
                    date_str=row.date_str,
                    user_id=participant.user_id,
                    source_type="reservation",
                    source_id=row.reservation_id,
                )
                for participant in sorted(
                    reservation_item.participants,
                    key=lambda item: item.user_id,
                )
            }
            row.daily_feed_results = [
                feed_results_by_user[participant.user_id].model_dump(mode="json")
                for participant in reservation_item.participants
            ]
        else:
            row.daily_feed_results = []
    elif row.outcome_snapshot != req.outcome_snapshot:
        # 相同 token 出现不同随机结果代表客户端状态已分叉，不能静默覆盖或假成功。
        return RoastReservationMutationResponse(ok=False)
    if row.status == "processing":
        row.status = "prepared"
    session.commit()
    return RoastReservationMutationResponse(ok=True, reservation=reservation_to_schema(session, row))


@router.post("/outcome/prepare", response_model=RoastReservationMutationResponse)
def prepare_outcome(req: RoastReservationOutcomeRequest, session: Session = Depends(get_session)):
    """SQLite 锁冲突时重跑完整结果结算，确保快照与加餐同时成功或同时回滚。"""

    return run_transaction_with_sqlite_lock_retry(
        session,
        lambda: _prepare_outcome_once(req, session),
    )


@router.post("/outcome", response_model=RoastReservationMutationResponse)
def save_outcome(req: RoastReservationOutcomeRequest, session: Session = Depends(get_session)):
    """兼容旧客户端：旧流程保存结果后会立刻发送，因此仍原子进入 sending。"""

    row = session.execute(
        select(RoastReservation).where(RoastReservation.reservation_id == req.reservation_id).with_for_update()
    ).scalar_one_or_none()
    if row is None or row.claim_token != req.claim_token or row.status not in {"processing", "sending", "completed"}:
        return RoastReservationMutationResponse(ok=False)
    if row.outcome_snapshot is None:
        if row.status != "processing":
            return RoastReservationMutationResponse(ok=False)
        row.outcome_snapshot = req.outcome_snapshot
    elif row.outcome_snapshot != req.outcome_snapshot:
        return RoastReservationMutationResponse(ok=False)
    if row.status == "processing":
        row.status = "sending"
    session.commit()
    return RoastReservationMutationResponse(ok=True, reservation=reservation_to_schema(session, row))


@router.post("/sending", response_model=RoastReservationMutationResponse)
def mark_sending(req: RoastReservationMutationRequest, session: Session = Depends(get_session)):
    """最终消息准备完成后进入不可回收状态；必须已有固定结果快照。"""

    row = session.execute(
        select(RoastReservation).where(RoastReservation.reservation_id == req.reservation_id).with_for_update()
    ).scalar_one_or_none()
    if (
        row is None
        or row.claim_token != req.claim_token
        or row.status not in {"prepared", "sending", "completed"}
        or row.outcome_snapshot is None
    ):
        return RoastReservationMutationResponse(ok=False)
    if row.status == "prepared":
        row.status = "sending"
    session.commit()
    return RoastReservationMutationResponse(ok=True, reservation=reservation_to_schema(session, row))


@router.post("/complete", response_model=RoastReservationMutationResponse)
def complete(req: RoastReservationCompleteRequest, session: Session = Depends(get_session)):
    """幂等完成预约，并在提供事件时与完成状态一起原子提交。"""

    row = session.execute(
        select(RoastReservation).where(RoastReservation.reservation_id == req.reservation_id).with_for_update()
    ).scalar_one_or_none()
    if (
        row is None
        or row.claim_token != req.claim_token
        or row.status not in {"sending", "completed"}
    ):
        return RoastReservationMutationResponse(ok=False)
    if row.status != "completed":
        row.status = "completed"
        row.completed_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    reservation_item = reservation_to_schema(session, row)
    event_recorded = False
    if req.event is not None:
        # 完成请求发生在外部消息已发送之后；即使客户端事件身份字段有误，也不能让
        # 预约永久卡在 sending。Cloud 覆盖身份字段后原子完成并记录结果。
        event_recorded = record_roast_event(
            session,
            bind_reservation_event(reservation_item, req.event),
            reservation=reservation_item,
        )
    session.commit()
    return RoastReservationMutationResponse(
        ok=True,
        reservation=reservation_item,
        event_recorded=event_recorded,
    )


@router.post("/release", response_model=RoastReservationMutationResponse)
def release(req: RoastReservationMutationRequest, session: Session = Depends(get_session)):
    row = session.execute(
        select(RoastReservation).where(RoastReservation.reservation_id == req.reservation_id).with_for_update()
    ).scalar_one_or_none()
    if row is None or row.claim_token != req.claim_token or row.status not in {"processing", "prepared"}:
        return RoastReservationMutationResponse(ok=False)
    row.status = "ready"
    row.claim_token = None
    row.claimed_at = None
    session.commit()
    return RoastReservationMutationResponse(ok=True, reservation=reservation_to_schema(session, row))
