import asyncio
import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.errors import Code, Error
from app.models import Run, utc_now

bearer = HTTPBearer(auto_error=False)


async def authorize(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> None:
    expected = request.app.state.settings.warmer_api_token.get_secret_value()
    if credentials is None or not secrets.compare_digest(credentials.credentials.encode(), expected.encode()):
        raise Error(Code.UNAUTHORIZED)


router = APIRouter(tags=["warmer"], dependencies=[Depends(authorize)])


@router.post("/extract-ip", status_code=202, response_model=Run, summary="Refresh the full POP inventory and verified IP mappings")
async def extract_ip(request: Request, response: Response):
    run = await request.app.state.jobs.start("extract_ip")
    response.headers["Location"] = f"/runs/{run.id}"
    return run


@router.post("/warm/{name}", status_code=202, response_model=Run, summary="Warm a registered query at every target POP")
async def warm(name: str, request: Request, response: Response):
    run = await request.app.state.jobs.start("warm", name)
    response.headers["Location"] = f"/runs/{run.id}"
    return run


@router.get("/runs/{run_id}", response_model=Run, summary="Inspect job progress and per-POP results")
async def get_run(run_id: str, request: Request):
    run = await asyncio.to_thread(request.app.state.store.get_run, run_id)
    if run is None:
        raise Error(Code.RUN_NOT_FOUND)
    return run


@router.get("/runs", response_model=list[Run], summary="List the twenty latest jobs")
async def list_runs(request: Request):
    return await asyncio.to_thread(request.app.state.store.latest_runs)


@router.get("/pops", summary="Inspect the target inventory, mappings and missing coverage")
async def get_pops(request: Request):
    inventory = await asyncio.to_thread(request.app.state.store.get_inventory)
    if inventory is None:
        return {"inventory": None, "complete": False, "target_count": 0, "missing_pops": []}
    max_age = timedelta(seconds=request.app.state.settings.warmer_mapping_max_age)
    fresh = utc_now() - inventory.fetched_at <= max_age
    covered = {m.pop for m in inventory.mappings if utc_now() - m.verified_at <= max_age}
    missing = sorted(p.code for p in inventory.pops if p.code not in covered)
    return {"inventory": inventory, "complete": fresh and not missing and not inventory.discovery_errors, "fresh": fresh,
            "target_count": len(inventory.pops), "missing_pops": missing}


@router.get("/queries", summary="List available named warming operations")
async def queries():
    return {"queries": ["articles", "picture"]}
