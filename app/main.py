import asyncio
import shutil
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.config import Settings
from app.database import Store
from app.discovery import Discovery
from app.errors import Error, handle_error, handle_unexpected_error, handle_validation_error
from app.jobs import Jobs
from app.observability import configure_logging, log_requests
from app.provider import EdgeClient
from app.routers import health, warmer
from app.telemetry import configure_telemetry

configure_logging()
OPENAPI_TAGS = [
    {"name": "health", "description": "Service liveness and local storage readiness."},
    {"name": "warmer", "description": "Authenticated discovery and named GraphQL cache warming."},
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    if not shutil.which("curl"):
        raise RuntimeError("curl is required for verified TLS connections to POP-specific IPs")
    store = Store(settings.warmer_state_db)
    await asyncio.to_thread(store.open)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=5), trust_env=False,
                                     follow_redirects=False, limits=httpx.Limits(max_connections=16)) as client:
            edge = EdgeClient(settings)
            app.state.settings, app.state.store = settings, store
            app.state.http_client = client
            app.state.jobs = Jobs(store, Discovery(client, edge, settings), edge, settings)
            try:
                yield
            finally:
                await app.state.jobs.shutdown()
    finally:
        await asyncio.to_thread(store.close)
        provider = getattr(app.state, "telemetry_provider", None)
        if provider:
            await asyncio.to_thread(provider.force_flush, 5000)


app = FastAPI(
    lifespan=lifespan,  # opens shared clients and persistent job storage, then closes them at shutdown
    title="Cosmofy Warmer API",
    summary="Warm named GraphQL queries across Stellate's edge network.",  # short explanation displayed near the API title
    description="Discovers and verifies POP addresses, runs named warming jobs, and reports complete or partial coverage.",  # longer explanation displayed in the API documentation
    version="1.0.0",
    openapi_tags=OPENAPI_TAGS,  # describes and orders endpoint groups in the documentation
    terms_of_service="https://github.com/Cosmofy/warmer",
    contact={"name": "Cosmofy", "url": "https://github.com/Cosmofy"},
    license_info={"name": "Proprietary"},
)
configure_telemetry(app)
app.middleware("http")(log_requests)
app.add_exception_handler(Error, handle_error)
app.add_exception_handler(RequestValidationError, handle_validation_error)
app.add_exception_handler(Exception, handle_unexpected_error)
app.include_router(health.router)
app.include_router(warmer.router)
