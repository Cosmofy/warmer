import asyncio
import threading
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.database import Store
from app.errors import Error
from app.jobs import Jobs
from app.models import EdgeResult, Inventory, Mapping, Pop, utc_now


def inventory():
    now = utc_now()
    return Inventory(fetched_at=now, source="test", pops=[Pop(code=p, city=p) for p in ["BOM", "LHR"]],
                     fastly_ranges=["151.101.0.0/16"],
                     mappings=[Mapping(pop=p, ip=ip, verified_at=now, server=f"cache-{p}")
                               for p, ip in [("BOM", "151.101.209.51"), ("LHR", "151.101.189.51")]])


@pytest.mark.parametrize("missing", [False, True])
def test_warm_keeps_entire_target_list(tmp_path, missing):
    async def scenario():
        store = Store(tmp_path / "state.db")
        store.open()
        data = inventory()
        if missing:
            data.mappings.pop()
        store.save_inventory(data)
        async def response(ip, ranges, operation, target):
            return EdgeResult(target_pop=target, actual_pop=target, ip=ip, cache_status="MISS", success=True)
        edge = AsyncMock()
        edge.request.side_effect = response
        jobs = Jobs(store, AsyncMock(), edge, Settings(warmer_api_token="x" * 32))
        run = await jobs.start("warm", "articles")
        await asyncio.gather(*jobs.tasks)
        finished = store.get_run(run.id)
        assert finished.target_pops == ["BOM", "LHR"]
        assert finished.status == ("incomplete" if missing else "complete")
        assert finished.missing_pops == (["LHR"] if missing else [])
        assert edge.request.await_count == (1 if missing else 2)
        store.close()
    asyncio.run(scenario())


def test_stale_inventory_rejected_before_any_request(tmp_path):
    async def scenario():
        store = Store(tmp_path / "state.db")
        store.open()
        data = inventory()
        data.fetched_at -= timedelta(days=3)
        store.save_inventory(data)
        edge = AsyncMock()
        jobs = Jobs(store, AsyncMock(), edge, Settings(warmer_api_token="x" * 32))
        with pytest.raises(Error):
            await jobs.start("warm", "articles")
        edge.request.assert_not_called()
        store.close()
    asyncio.run(scenario())


def test_discovery_failure_does_not_replace_existing_inventory(tmp_path):
    async def scenario():
        store = Store(tmp_path / "state.db")
        store.open()
        data = inventory()
        store.save_inventory(data)
        discovery = AsyncMock()
        discovery.refresh.side_effect = RuntimeError("provider down")
        jobs = Jobs(store, discovery, AsyncMock(), Settings(warmer_api_token="x" * 32))
        run = await jobs.start("extract_ip")
        await asyncio.gather(*jobs.tasks)
        assert store.get_run(run.id).status == "failed"
        assert store.get_inventory() == data
        store.close()
    asyncio.run(scenario())


def test_cancelled_http_acceptance_still_starts_the_reserved_job(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path / "state.db")
        store.open()
        entered, release = threading.Event(), threading.Event()
        original = store.reserve_run
        def delayed(*args):
            value = original(*args)
            entered.set()
            assert release.wait(5)
            return value
        monkeypatch.setattr(store, "reserve_run", delayed)
        discovery = AsyncMock()
        discovery.refresh.return_value = inventory()
        jobs = Jobs(store, discovery, AsyncMock(), Settings(warmer_api_token="x" * 32))
        caller = asyncio.create_task(jobs.start("extract_ip"))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            release.set()
            await asyncio.gather(*list(jobs._admissions))
            await asyncio.gather(*list(jobs.tasks))
            assert store.latest_runs()[0].status == "complete"
            assert not jobs.tasks
        finally:
            release.set()
            await jobs.shutdown()
            store.close()
    asyncio.run(scenario())


def test_cancelled_checkpoint_finishes_before_terminal_write(tmp_path, monkeypatch):
    async def scenario():
        store = Store(tmp_path / "state.db")
        store.open()
        data = inventory()
        data.pops = data.pops[:1]
        data.mappings = data.mappings[:1]
        store.save_inventory(data)
        entered, release = threading.Event(), threading.Event()
        original = store.save_run
        def delayed(run):
            if run.status == "running":
                entered.set()
                assert release.wait(5)
            original(run)
        monkeypatch.setattr(store, "save_run", delayed)
        edge = AsyncMock()
        edge.request.return_value = EdgeResult(target_pop="BOM", actual_pop="BOM", success=True)
        jobs = Jobs(store, AsyncMock(), edge, Settings(warmer_api_token="x" * 32))
        try:
            run = await jobs.start("warm", "articles")
            assert await asyncio.to_thread(entered.wait, 5)
            task = next(iter(jobs.tasks))
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            await task
            assert store.get_run(run.id).status == "interrupted"
            assert store.get_run(run.id).covered_pops == ["BOM"]
            # Admission is not stuck behind a resurrected active row.
            new, _ = store.reserve_run("warm", "articles", 90000)
            assert new.id != run.id
        finally:
            release.set()
            await jobs.shutdown()
            store.close()
    asyncio.run(scenario())


def test_discovery_handoff_reserves_current_full_inventory(tmp_path):
    async def scenario():
        store = Store(tmp_path / "state.db")
        store.open()
        old = inventory()
        old.pops = old.pops[:1]
        old.mappings = old.mappings[:1]
        store.save_inventory(old)
        entered, release = asyncio.Event(), asyncio.Event()
        async def refresh(previous):
            entered.set()
            await release.wait()
            return inventory()
        discovery = AsyncMock()
        discovery.refresh.side_effect = refresh
        jobs = Jobs(store, discovery, AsyncMock(), Settings(warmer_api_token="x" * 32))
        try:
            await jobs.start("extract_ip")
            await entered.wait()
            with pytest.raises(Error) as busy:
                await jobs.start("warm", "articles")
            assert busy.value.code.name == "RUN_IN_PROGRESS"
            release.set()
            await asyncio.gather(*list(jobs.tasks))
            run, snapshot = await asyncio.to_thread(store.reserve_run, "warm", "articles", 90000)
            assert run.target_pops == ["BOM", "LHR"]
            assert run.missing_pops == ["BOM", "LHR"]
            assert len(snapshot.pops) == 2
        finally:
            release.set()
            await jobs.shutdown()
            store.close()
    asyncio.run(scenario())
