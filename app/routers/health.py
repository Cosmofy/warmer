import asyncio
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", status_code=200, summary="App liveness check")
async def live(request: Request):
    return {"status": "ok", "app": request.app.title, "version": request.app.version}


@router.get("/ready", summary="Check local job storage readiness")
async def ready(request: Request):
    try:
        async with asyncio.timeout(2):
            healthy = await asyncio.to_thread(request.app.state.store.healthy)
    except (sqlite3.Error, TimeoutError, OSError):
        healthy = False
    return JSONResponse(status_code=200 if healthy else 503,
                        content={"status": "ready" if healthy else "not_ready",
                                 "dependencies": {"storage": "ok" if healthy else "unavailable"}})
