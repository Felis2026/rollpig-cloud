from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..models import GroupProtection
from ..schemas import ProtectedResponse, ReplaceGroupProtectionsRequest

router = APIRouter(prefix="/v1/protections", tags=["protections"], dependencies=[Depends(verify_token)])


@router.post("/replace-group")
def replace_group(req: ReplaceGroupProtectionsRequest, session: Session = Depends(get_session)):
    session.execute(
        delete(GroupProtection).where(
            GroupProtection.group_id == req.group_id,
            GroupProtection.protect_date == req.protect_date,
        )
    )
    for user_id in sorted({str(user_id) for user_id in req.user_ids if user_id}):
        session.add(GroupProtection(protect_date=req.protect_date, group_id=req.group_id, user_id=user_id))
    session.commit()
    return {"ok": True}


@router.get("/check", response_model=ProtectedResponse)
def check_protected(protect_date: dt.date, group_id: str, user_id: str, session: Session = Depends(get_session)):
    row = session.execute(
        select(GroupProtection).where(
            GroupProtection.protect_date == protect_date,
            GroupProtection.user_id == user_id,
            or_(GroupProtection.group_id == group_id, GroupProtection.group_id == "__all__"),
        )
    ).scalar_one_or_none()
    return ProtectedResponse(protected=bool(row))
