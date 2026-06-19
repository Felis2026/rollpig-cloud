from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import init_db
from .routers.catalog import router as catalog_router
from .routers.collections import router as collections_router
from .routers.cooldowns import router as cooldowns_router
from .routers.daily_rolls import router as daily_rolls_router
from .routers.draw_state import router as draw_state_router
from .routers.events import router as events_router
from .routers.group_rolls import router as group_rolls_router
from .routers.groups import router as groups_router
from .routers.protections import router as protections_router

app = FastAPI(title="rollpig-cloud", version="0.2.0")
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
RESOURCES_DIR = STATIC_DIR / "resources"


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
app.include_router(catalog_router)

if RESOURCES_DIR.exists():
    # /resources 用于托管 RollPig 小猪静态资源包。这里不挂载 Python 代码，只暴露 json/png。
    app.mount("/resources", StaticFiles(directory=RESOURCES_DIR), name="resources")
