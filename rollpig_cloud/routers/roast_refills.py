from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..schemas import (
    GroupActiveUsersMarkRequest,
    GroupActiveUsersResponse,
    GroupRoastRefillBindRequest,
    GroupRoastRefillCompleteRequest,
    GroupRoastRefillCompleteResponse,
    GroupRoastRefillFailRequest,
    GroupRoastRefillLookupResponse,
    GroupRoastRefillPrepareRequest,
    GroupRoastRefillPrepareResponse,
    SimpleAllowedResponse,
)
from ..services.roast_refills import (
    bind_refill_message,
    complete_refill,
    fail_refill,
    get_active_refill,
    get_group_active_user_ids,
    mark_group_active_users,
    prepare_refill,
    refill_to_schema,
)


router = APIRouter(
    prefix="/v1/group-roast-refills",
    tags=["group-roast-refills"],
    dependencies=[Depends(verify_token)],
)


# ================================ 群日活 ================================ #

@router.post("/active-users/mark", response_model=SimpleAllowedResponse)
def mark_active_users(req: GroupActiveUsersMarkRequest, session: Session = Depends(get_session)):
    mark_group_active_users(
        session,
        date_str=req.date_str,
        group_id=req.group_id,
        user_ids=req.user_ids,
    )
    session.commit()
    return SimpleAllowedResponse(allowed=True)


@router.get("/active-users", response_model=GroupActiveUsersResponse)
def list_active_users(
    group_id: str,
    date_str: dt.date,
    session: Session = Depends(get_session),
    cutoff_at: dt.datetime | None = None,
):
    return GroupActiveUsersResponse(
        user_ids=get_group_active_user_ids(
            session,
            date_str,
            group_id,
            cutoff_at=cutoff_at,
        )
    )


# ================================ 申请生命周期 ================================ #

@router.post("/prepare", response_model=GroupRoastRefillPrepareResponse)
def prepare(req: GroupRoastRefillPrepareRequest, session: Session = Depends(get_session)):
    # 无行可锁时由 active_key 唯一键裁决；冲突实例回滚后重查已存在申请。
    for _ in range(3):
        try:
            response = prepare_refill(session, req)
            session.commit()
            return response
        except IntegrityError:
            session.rollback()
    raise RuntimeError("prepare group roast refill retry exhausted")


@router.post("/bind-message", response_model=GroupRoastRefillLookupResponse)
def bind_message(req: GroupRoastRefillBindRequest, session: Session = Depends(get_session)):
    row = bind_refill_message(
        session,
        request_id=req.request_id,
        message_id=req.message_id,
    )
    session.commit()
    return GroupRoastRefillLookupResponse(request=refill_to_schema(row) if row else None)


@router.get("/active", response_model=GroupRoastRefillLookupResponse)
def get_active(
    group_id: str,
    date_str: dt.date,
    now_ts: float | None = None,
    session: Session = Depends(get_session),
):
    row = get_active_refill(
        session,
        date_str=date_str,
        group_id=group_id,
        now_ts=now_ts,
    )
    session.commit()
    return GroupRoastRefillLookupResponse(request=refill_to_schema(row) if row else None)


@router.post("/fail", response_model=SimpleAllowedResponse)
def fail(req: GroupRoastRefillFailRequest, session: Session = Depends(get_session)):
    changed = fail_refill(
        session,
        request_id=req.request_id,
        message_id=req.message_id,
        reason=req.reason,
    )
    session.commit()
    return SimpleAllowedResponse(allowed=changed)


@router.post("/complete", response_model=GroupRoastRefillCompleteResponse)
def complete(req: GroupRoastRefillCompleteRequest, session: Session = Depends(get_session)):
    response = complete_refill(
        session,
        request_id=req.request_id,
        message_id=req.message_id,
        voter_ids=req.voter_ids,
        excluded_user_ids=req.excluded_user_ids,
        max_charges=req.max_charges,
        now_ts=req.now_ts,
    )
    session.commit()
    return response
