from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from . import __version__
from .db import init_db
from .routers.catalog import router as catalog_router
from .routers.collections import router as collections_router
from .routers.cooldowns import router as cooldowns_router
from .routers.daily_rolls import router as daily_rolls_router
from .routers.daily_reports import router as daily_reports_router
from .routers.draw_state import router as draw_state_router
from .routers.events import router as events_router
from .routers.group_rolls import router as group_rolls_router
from .routers.groups import router as groups_router
from .routers.protections import router as protections_router
from .routers.roast_reservations import router as roast_reservations_router
from .routers.roast_refills import router as roast_refills_router
from .services.key_usage import record_key_request

app = FastAPI(title="rollpig-cloud", version=__version__)
logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
RESOURCES_DIR = STATIC_DIR / "resources"


# ================================ 具名 Key 访问记录 ================================ #


def _request_operation(request: Request) -> str:
    """优先使用路由模板，避免把用户 ID 等动态路径写进日志与统计。"""

    route = request.scope.get("route")
    path = str(getattr(route, "path", request.url.path))
    return f"{request.method.upper()} {path}"


async def _record_authenticated_request(request: Request, status_code: int, started_at: float) -> None:
    identity = getattr(request.state, "api_key_identity", None)
    if identity is None:
        return
    operation = _request_operation(request)
    payload = {
        "event": "authenticated_request",
        "key_id": identity.key_id,
        "key_name": identity.name,
        "operation": operation,
        "status": status_code,
        "duration_ms": round((time.perf_counter() - started_at) * 1000, 2),
    }
    logger.info("rollpig_cloud_access %s", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    try:
        await asyncio.to_thread(
            record_key_request,
            identity,
            method=request.method,
            operation=operation,
            status_code=status_code,
        )
    except Exception:
        # 使用统计是旁路能力；数据库统计故障不能把已成功的业务响应改成 500。
        logger.exception("failed to record source key usage: key_id=%s operation=%s", identity.key_id, operation)


@app.middleware("http")
async def track_authenticated_requests(request: Request, call_next):
    started_at = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        await _record_authenticated_request(request, 500, started_at)
        raise
    await _record_authenticated_request(request, response.status_code, started_at)
    return response


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.include_router(daily_rolls_router)
app.include_router(daily_reports_router)
app.include_router(draw_state_router)
app.include_router(group_rolls_router)
app.include_router(collections_router)
app.include_router(cooldowns_router)
app.include_router(events_router)
app.include_router(protections_router)
app.include_router(groups_router)
app.include_router(catalog_router)
app.include_router(roast_reservations_router)
app.include_router(roast_refills_router)

if RESOURCES_DIR.exists():
    # /resources 用于托管 RollPig 小猪静态资源包。这里不挂载 Python 代码，只暴露 json/png。
    app.mount("/resources", StaticFiles(directory=RESOURCES_DIR), name="resources")
