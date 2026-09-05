import asyncio
import logging
from datetime import timedelta

from opentelemetry import trace

from app.config import Settings
from app.database import Store
from app.discovery import Discovery
from app.errors import Code, Error
from app.models import EdgeResult, Inventory, Run, utc_now
from app.observability import run_id_context
from app.provider import EdgeClient
from app.queries import Operation, resolve_operation

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


async def durable_call(function, *args):
    """Finish an already-started SQLite write before propagating cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


class Jobs:
    def __init__(self, store: Store, discovery: Discovery, edge: EdgeClient, settings: Settings):
        self.store, self.discovery, self.edge, self.settings = store, discovery, edge, settings
        self.tasks: set[asyncio.Task] = set()
        self._admissions: set[asyncio.Task] = set()
        self._closing = False
        self._write_lock = asyncio.Lock()

    async def start(self, operation: str, name: str | None = None) -> Run:
        query = resolve_operation(name) if operation == "warm" else None
        if self._closing:
            raise Error(Code.SHUTTING_DOWN)
        admission = asyncio.create_task(self._accept(operation, name, query))
        self._admissions.add(admission)
        admission.add_done_callback(self._admission_done)
        # A disconnected HTTP caller must not leave an inserted job without a task.
        return await asyncio.shield(admission)

    def _admission_done(self, task: asyncio.Task) -> None:
        self._admissions.discard(task)
        if not task.cancelled():
            task.exception()  # retrieve failures even if the HTTP caller disconnected

    async def _accept(self, operation: str, name: str | None, query: Operation | None) -> Run:
        run, inventory = await asyncio.to_thread(self.store.reserve_run, operation, name, self.settings.warmer_mapping_max_age)
        # Return a snapshot; background updates cannot mutate the 202 response.
        accepted = run.model_copy(deep=True)
        task = asyncio.create_task(self._execute(run, inventory, query), name=f"warmer-{run.id}")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return accepted

    async def _execute(self, run: Run, inventory: Inventory | None, query: Operation | None) -> None:
        token = run_id_context.set(run.id)
        try:
            with tracer.start_as_current_span(f"warmer.{run.operation}") as span:
                span.set_attribute("warmer.run_id", run.id)
                try:
                    async with asyncio.timeout(self.settings.warmer_job_timeout):
                        if run.operation == "extract_ip":
                            updated = await self.discovery.refresh(inventory)
                            await durable_call(self.store.save_inventory, updated)
                            run.target_pops = sorted(p.code for p in updated.pops)
                            run.finish_coverage({m.pop for m in updated.mappings})
                            if updated.discovery_errors:
                                run.status = "incomplete"
                                run.error = "discovery_errors_present"
                        else:
                            await self._warm(run, inventory, query)
                            run.finish_coverage(set(run.covered_pops))
                except asyncio.CancelledError:
                    run.status, run.error = "interrupted", "service_shutdown"
                except TimeoutError:
                    run.status, run.error = "incomplete", "job_deadline_exceeded"
                except Exception:
                    # A job boundary records failure rather than leaving a job permanently running.
                    logger.exception("warmer job failed", extra={"event": "warmer.job.failed", "run_id": run.id})
                    run.status, run.error = "failed", "job_failed"
                run.finished_at = utc_now()
                run.missing_pops = sorted(set(run.target_pops) - set(run.covered_pops))
                await durable_call(self.store.save_run, run.model_copy(deep=True))
                logger.info("warmer job finished", extra={"event": "warmer.job.finished", "run_id": run.id,
                            "status": run.status, "target_count": len(run.target_pops),
                            "covered_count": len(run.covered_pops), "missing_count": len(run.missing_pops)})
                span.set_attribute("warmer.status", run.status)
        finally:
            run_id_context.reset(token)

    async def _record(self, run: Run, result: EdgeResult) -> None:
        async with self._write_lock:
            run.results.append(result)
            if result.success and result.target_pop not in run.covered_pops:
                run.covered_pops.append(result.target_pop)
            run.missing_pops = sorted(set(run.target_pops) - set(run.covered_pops))
            await durable_call(self.store.save_run, run.model_copy(deep=True))
        logger.info("edge warming request finished", extra={"event": "warmer.edge.finished", "run_id": run.id,
                    "target_pop": result.target_pop, "actual_pop": result.actual_pop,
                    "cache_status": result.cache_status, "duration_ms": result.duration_ms,
                    "outcome": "success" if result.success else result.error})

    async def _warm(self, run: Run, inventory: Inventory, query: Operation) -> None:
        gate = asyncio.Semaphore(self.settings.warmer_concurrency)

        async def warm_pop(pop: str) -> None:
            async with gate:
                candidates = [m for m in inventory.mappings if m.pop == pop and
                              utc_now() - m.verified_at <= timedelta(seconds=self.settings.warmer_mapping_max_age)]
                if not candidates:
                    await self._record(run, EdgeResult(target_pop=pop, error="missing_fresh_mapping"))
                    return
                for candidate in candidates[:3]:
                    result = await self.edge.request(candidate.ip, inventory.fastly_ranges, query, pop)
                    await self._record(run, result)
                    if result.success:
                        break

        async with asyncio.TaskGroup() as group:
            for pop in run.target_pops:
                group.create_task(warm_pop(pop))
        run.results.sort(key=lambda result: (result.target_pop or "", result.ip or ""))

    async def shutdown(self) -> None:
        self._closing = True
        # Let admission finish before cancelling workers or closing their database.
        await asyncio.gather(*list(self._admissions), return_exceptions=True)
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
