"""Collector-friendly JSON logs; callers must use static messages, never payloads."""

import json
import logging
import math
import os
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from socket import gethostname
from threading import Lock
from time import perf_counter
from uuid import uuid4

from fastapi import Request, Response
from opentelemetry import trace
from opentelemetry.sdk.resources import OTELResourceDetector
from starlette.middleware.base import RequestResponseEndpoint

LOG_FIELDS = (
    "event",
    "action",
    "dependency",
    "source",
    "provider",
    "operation",
    "query_name",
    "run_id",
    "target_pop",
    "actual_pop",
    "status",
    "outcome",
    "coverage",
    "coverage_percent",
    "complete",
    "count",
    "target_count",
    "total_count",
    "covered_count",
    "success_count",
    "failure_count",
    "failed_count",
    "missing_count",
    "attempted_count",
    "candidate_count",
    "verified_count",
    "attempt",
    "cache_status",
    "error_code",
    "exception_type",
    "request_id",
    "http_method",
    "http_path",
    "http_status_code",
    "duration_ms",
)
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
run_id_context: ContextVar[str | None] = ContextVar("run_id", default=None)
_logging_lock = Lock()
logger = logging.getLogger(__name__)


def valid_request_id(value: object) -> str | None:
    """Reject, rather than truncate, ambiguous or unsafe correlation identifiers."""
    return (
        value
        if isinstance(value, str) and REQUEST_ID_PATTERN.fullmatch(value)
        else None
    )


def service_identity() -> dict[str, str]:
    node = os.getenv("OTEL_NODE_NAME", "").strip() or gethostname()
    return {
        "service.name": "warmer",
        "service.namespace": "cosmofy",
        "service.instance.id": node,
        "host.name": node,
        **OTELResourceDetector().detect().attributes,
    }


class JsonFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__()
        self.identity = service_identity()

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "service_namespace": self.identity["service.namespace"],
            "service": self.identity["service.name"],
            "service_instance_id": self.identity["service.instance.id"],
            "host_name": self.identity["host.name"],
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": valid_request_id(request_id_context.get()),
            "trace_id": None,
            "span_id": None,
        }
        if (run_id := run_id_context.get()) is not None:
            payload["run_id"] = run_id
        # Deliberately omit arbitrary extras, headers, URLs, bodies and client origin.
        for field in LOG_FIELDS:
            if not hasattr(record, field):
                continue
            value = getattr(record, field)
            if field == "request_id":
                value = valid_request_id(value)
            if (
                value is None
                or isinstance(value, (str, bool, int))
                or isinstance(value, float)
                and math.isfinite(value)
            ):
                payload[field] = value

        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            payload["trace_id"] = format(context.trace_id, "032x")
            payload["span_id"] = format(context.span_id, "016x")

        # HTTPX/curl exceptions can contain credentials, URLs or GraphQL bodies.
        # Keep the failure type, never exception messages, locals or stack text.
        if record.exc_info and record.exc_info[0]:
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, separators=(",", ":"), allow_nan=False)


def configure_logging() -> None:
    with _logging_lock:
        app_logger = logging.getLogger("app")
        app_logger.setLevel(logging.INFO)
        app_logger.propagate = False
        if any(handler.get_name() == "cosmofy-json" for handler in app_logger.handlers):
            return
        handler = logging.StreamHandler(sys.stdout)
        handler.set_name("cosmofy-json")
        handler.setFormatter(JsonFormatter())
        app_logger.addHandler(handler)


async def log_requests(
    request: Request, call_next: RequestResponseEndpoint
) -> Response:
    identifiers = request.headers.getlist("x-request-id")
    request_id = (
        valid_request_id(identifiers[0]) if len(identifiers) == 1 else None
    ) or uuid4().hex
    request.state.request_id = request_id
    token = request_id_context.set(request_id)
    started_at = perf_counter()
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("request_id", request_id)

    def fields(status_code: int, event: str) -> dict[str, object]:
        route = request.scope.get("route")
        # Templates bound cardinality and avoid reflecting arbitrary unmatched paths.
        path = getattr(route, "path", None) or "<unmatched>"
        return {
            "event": event,
            "request_id": request_id,
            "http_method": request.method,
            "http_path": path,
            "http_status_code": status_code,
            "duration_ms": round((perf_counter() - started_at) * 1000, 3),
        }

    try:
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "http request failed", extra=fields(500, "http.request.failed")
            )
            raise
        response.headers["x-request-id"] = request_id
        logger.info(
            "http request completed",
            extra=fields(response.status_code, "http.request.completed"),
        )
        return response
    finally:
        request_id_context.reset(token)
