from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..models import Collection
from ..schemas import CollectionResponse

router = APIRouter(prefix="/v1/collections", tags=["collections"], dependencies=[Depends(verify_token)])


@router.get("", response_model=CollectionResponse)
def get_collection(user_id: str, session: Session = Depends(get_session)):
    rows = session.execute(select(Collection).where(Collection.user_id == user_id)).scalars().all()
    pig_ids = sorted({row.pig_id for row in rows})
    return CollectionResponse(pig_ids=pig_ids)
