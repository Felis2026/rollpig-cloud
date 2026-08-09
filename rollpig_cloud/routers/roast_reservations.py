from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..models import RoastReservation, UnrolledRoastAttempt
from ..schemas import (
    RoastReservationClaimRequest,
    RoastReservationClaimResponse,
    RoastReservationMutationRequest,
    RoastReservationMutationResponse,
    RoastReservationOutcomeRequest,
    RoastReservationOwnedResponse,
    RoastReservationPrepareRequest,
    RoastReservationPrepareResponse,
    UnrolledRoastAttemptRequest,
    UnrolledRoastAttemptResponse,
)
from ..services.reservations import activate_if_target_already_rolled, prepare_reservation, reservation_to_schema


router = APIRouter(
    prefix="/v1/roast-reservations",
    tags=["roast-reservations"],
    dependencies=[Depends(verify_token)],
)
ROAST_RESERVATION_CLAIM_TIMEOUT = dt.timedelta(minutes=5)


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
            RoastReservation.status.in_(("pending", "ready", "processing")),
        ).limit(1)
    ).first()
    return RoastReservationOwnedResponse(has_owned=exists is not None)


@router.post("/claim", response_model=RoastReservationClaimResponse)
def claim(req: RoastReservationClaimRequest, session: Session = Depends(get_session)):
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    stale_before = now - ROAST_RESERVATION_CLAIM_TIMEOUT
    rows = session.execute(
        select(RoastReservation)
        .where(
            RoastReservation.delivery_bot_id == req.delivery_bot_id,
            RoastReservation.date_str == req.date_str,
            (
                (RoastReservation.status == "ready")
                | (
                    (RoastReservation.status == "processing")
                    # processing 只代表发送前租约。已有结果快照的旧记录可能已经发出，
                    # 即使状态尚未迁移也不能再自动领取。
                    & RoastReservation.outcome_snapshot.is_(None)
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
    ).scalars().all()
    for row in rows:
        # 明确发送失败会把固定快照保留并退回 ready；重领时直接进入 sending，
        # 这样客户端在真正发送前崩溃也不会让一条可能已发送的消息被租约回收。
        row.status = "sending" if row.outcome_snapshot is not None else "processing"
        row.claim_token = uuid.uuid4().hex
        row.claimed_at = now
    session.flush()
    items = [reservation_to_schema(session, row) for row in rows]
    session.commit()
    return RoastReservationClaimResponse(items=items)


@router.post("/outcome", response_model=RoastReservationMutationResponse)
def save_outcome(req: RoastReservationOutcomeRequest, session: Session = Depends(get_session)):
    row = session.execute(
        select(RoastReservation).where(RoastReservation.reservation_id == req.reservation_id).with_for_update()
    ).scalar_one_or_none()
    if row is None or row.claim_token != req.claim_token or row.status not in {"processing", "sending", "completed"}:
        return RoastReservationMutationResponse(ok=False)
    if row.outcome_snapshot is None:
        row.outcome_snapshot = req.outcome_snapshot
    if row.status == "processing":
        # 固化结果与进入发送态必须同事务提交，避免发送成功后仍被 processing 租约回收。
        row.status = "sending"
    session.commit()
    return RoastReservationMutationResponse(ok=True, reservation=reservation_to_schema(session, row))


@router.post("/complete", response_model=RoastReservationMutationResponse)
def complete(req: RoastReservationMutationRequest, session: Session = Depends(get_session)):
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
    session.commit()
    return RoastReservationMutationResponse(ok=True, reservation=reservation_to_schema(session, row))


@router.post("/release", response_model=RoastReservationMutationResponse)
def release(req: RoastReservationMutationRequest, session: Session = Depends(get_session)):
    row = session.execute(
        select(RoastReservation).where(RoastReservation.reservation_id == req.reservation_id).with_for_update()
    ).scalar_one_or_none()
    if row is None or row.claim_token != req.claim_token or row.status not in {"processing", "sending"}:
        return RoastReservationMutationResponse(ok=False)
    row.status = "ready"
    row.claim_token = None
    row.claimed_at = None
    session.commit()
    return RoastReservationMutationResponse(ok=True, reservation=reservation_to_schema(session, row))
