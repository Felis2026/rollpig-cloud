from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..auth import verify_token
from ..db import get_session
from ..schemas import DrawStateResponse
from ..services.progress import build_draw_state_response

router = APIRouter(prefix="/v1/draw-state", tags=["draw-state"], dependencies=[Depends(verify_token)])


@router.get("", response_model=DrawStateResponse)
def get_draw_state(user_id: str, session: Session = Depends(get_session)):
    return build_draw_state_response(session, user_id)
