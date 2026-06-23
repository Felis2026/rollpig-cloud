from __future__ import annotations

from fastapi import FastAPI

from .db import init_db
from .routers.collections import router as collections_router
from .routers.cooldowns import router as cooldowns_router
from .routers.daily_rolls import router as daily_rolls_router
from .routers.draw_state import router as draw_state_router
from .routers.events import router as events_router
from .routers.group_rolls import router as group_rolls_router
from .routers.groups import router as groups_router
from .routers.protections import router as protections_router

app = FastAPI(title="rollpig-cloud", version="0.1.0")


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.include_router(daily_rolls_router)
app.include_router(draw_state_router)
app.include_router(group_rolls_router)
app.include_router(collections_router)
app.include_router(cooldowns_router)
app.include_router(events_router)
app.include_router(protections_router)
app.include_router(groups_router)
