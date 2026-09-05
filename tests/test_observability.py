import asyncio
import io
import json
import logging
import sys
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from app import observability
from app.observability import (
    JsonFormatter,
    log_requests,
    request_id_context,
    run_id_context,
)


@pytest.fixture(autouse=True)
def clean_identity(monkeypatch):
    for key in ("OTEL_RESOURCE_ATTRIBUTES", "OTEL_SERVICE_NAME", "OTEL_NODE_NAME"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = observability.logger
    original = logger.handlers[:], logger.level, logger.propagate
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    logger.handlers, logger.level, logger.propagate = original
    handler.close()


def format_log(exc_info=None, **extra):
    record = logging.LogRecord(
        "app.test", logging.INFO, __file__, 1, "test message", (), exc_info
    )
    record.__dict__.update(extra)
    return json.loads(JsonFormatter().format(record))


@pytest.mark.parametrize("flags", [TraceFlags.DEFAULT, TraceFlags.SAMPLED])
def test_log_identity_and_sampled_or_unsampled_correlation(flags):
    span = NonRecordingSpan(SpanContext(0x1234, 0x5678, False, flags))
    with trace.use_span(span, end_on_exit=False):
        payload = format_log(event="warmer.run.completed", duration_ms=1.25)
    assert payload["service"] == "warmer"
    assert payload["service_namespace"] == "cosmofy"
    assert payload["service_instance_id"]
    assert payload["host_name"]
    assert payload["trace_id"] == "00000000000000000000000000001234"
    assert payload["span_id"] == "0000000000005678"
    assert payload["timestamp"].endswith("Z")
    assert payload["event"] == "warmer.run.completed"
    assert payload["duration_ms"] == 1.25


def test_no_span_has_explicit_null_correlation():
    payload = format_log()
    assert payload["trace_id"] is payload["span_id"] is payload["request_id"] is None


def test_standard_identity_overrides(monkeypatch):
    monkeypatch.setenv("OTEL_NODE_NAME", "warmer-node-a")
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES",
        "service.instance.id=instance-a,service.namespace=custom",
    )
    monkeypatch.setenv("OTEL_SERVICE_NAME", "custom-warmer")
    payload = format_log()
    assert payload["service"] == "custom-warmer"
    assert payload["service_instance_id"] == "instance-a"
    assert payload["host_name"] == "warmer-node-a"
    assert payload["service_namespace"] == "custom"


def test_request_and_run_context_and_explicit_override():
    token = request_id_context.set("context-id")
    run_token = run_id_context.set("run-1")
    try:
        assert format_log()["request_id"] == "context-id"
        assert format_log()["run_id"] == "run-1"
        assert format_log(request_id="record-id")["request_id"] == "record-id"
        assert format_log(request_id="bad\nid")["request_id"] is None
    finally:
        request_id_context.reset(token)
        run_id_context.reset(run_token)


def test_operational_fields_exclude_secrets_bodies_and_spoofed_origin():
    expected = {
        "event": "warmer.target.completed",
        "run_id": "run-1",
        "query_name": "articles",
        "target_pop": "LHR",
        "actual_pop": "YYZ",
        "coverage": 0.5,
        "count": 2,
        "target_count": 4,
        "covered_count": 2,
        "missing_count": 1,
        "failed_count": 1,
        "complete": False,
        "duration_ms": 10.2,
        "error_code": "pop_mismatch",
    }
    payload = format_log(
        **expected,
        authorization="secret",
        query="query { private }",
        body="private body",
        headers={"x-api-key": "secret"},
        client_ip="203.0.113.2",
        country="GB",
        command="curl secret",
    )
    assert expected.items() <= payload.items()
    assert (
        not {
            "authorization",
            "query",
            "body",
            "headers",
            "client_ip",
            "country",
            "command",
        }
        & payload.keys()
    )


def test_exception_text_stack_and_complex_extras_are_not_logged():
    try:
        raise RuntimeError("authorization=secret query { private }")
    except RuntimeError:
        payload = format_log(
            exc_info=sys.exc_info(), coverage=float("nan"), count={"secret": 1}
        )
    assert payload["exception_type"] == "RuntimeError"
    assert "exception" not in payload
    assert "secret" not in json.dumps(payload)
    assert "coverage" not in payload
    assert "count" not in payload


def test_configure_logging_is_idempotent_and_preserves_other_handlers():
    logger = logging.getLogger("app")
    original = logger.handlers[:], logger.level, logger.propagate
    other = logging.NullHandler()
    logger.handlers = [other]
    try:
        observability.configure_logging()
        observability.configure_logging()
        assert len(logger.handlers) == 2
        assert logger.handlers[0] is other
        assert logger.handlers[1].get_name() == "cosmofy-json"
        assert isinstance(logger.handlers[1].formatter, JsonFormatter)
        assert logger.level == logging.INFO
        assert logger.propagate is False
    finally:
        for handler in logger.handlers:
            if handler is not other:
                handler.close()
        logger.handlers, logger.level, logger.propagate = original


@pytest.mark.parametrize(
    "supplied,accepted",
    [
        ("edge-1._abc", True),
        ("x" * 128, True),
        (None, False),
        ("", False),
        ("x" * 129, False),
        ("line\r\nbreak", False),
        ("id with space", False),
        ("café", False),
        ("id,another", False),
        ("/path", False),
        (" id", False),
    ],
)
def test_request_id_validation_response_and_no_sensitive_logging(
    supplied, accepted, logs
):
    headers = [
        (b"authorization", b"Bearer secret"),
        (b"forwarded", b"for=203.0.113.3"),
        (b"x-forwarded-for", b"203.0.113.4"),
        (b"cf-ipcountry", b"GB"),
    ]
    if supplied is not None:
        headers.append((b"x-request-id", supplied.encode("latin-1")))
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/warm/articles",
            "query_string": b"query=private",
            "headers": headers,
            "client": ("127.0.0.1", 123),
            "route": SimpleNamespace(path="/warm/{name}"),
        }
    )

    async def call_next(req):
        assert request_id_context.get() == req.state.request_id
        return Response(status_code=202)

    response = asyncio.run(log_requests(request, call_next))
    identifier = response.headers["x-request-id"]
    assert observability.valid_request_id(identifier)
    assert (identifier == supplied) is accepted
    assert request_id_context.get() is None
    payload = logs()[0]
    assert payload["request_id"] == identifier
    assert payload["http_path"] == "/warm/{name}"
    assert payload["http_status_code"] == 202
    assert payload["duration_ms"] >= 0
    assert "secret" not in json.dumps(payload)
    assert "private" not in json.dumps(payload)
    assert "203.0.113" not in json.dumps(payload)
    assert "client_ip" not in payload


def test_duplicate_request_ids_are_replaced(logs):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-request-id", b"first"), (b"x-request-id", b"second")],
        }
    )

    async def call_next(req):
        return Response()

    response = asyncio.run(log_requests(request, call_next))
    assert response.headers["x-request-id"] not in {"first", "second"}
    assert logs()[0]["http_path"] == "<unmatched>"


def test_exception_is_reraised_and_context_restored(logs):
    request = Request(
        {"type": "http", "method": "POST", "path": "/warm/articles", "headers": []}
    )
    error = RuntimeError("private query and password")

    async def call_next(req):
        raise error

    token = request_id_context.set("outer-id")
    try:
        with pytest.raises(RuntimeError) as raised:
            asyncio.run(log_requests(request, call_next))
        assert raised.value is error
        assert request_id_context.get() == "outer-id"
    finally:
        request_id_context.reset(token)
    payload = logs()[0]
    assert payload["event"] == "http.request.failed"
    assert payload["http_status_code"] == 500
    assert payload["level"] == "ERROR"
    assert payload["exception_type"] == "RuntimeError"
    assert "password" not in json.dumps(payload)


def test_concurrent_requests_keep_context_isolated(logs):
    app = FastAPI()
    app.middleware("http")(log_requests)

    @app.get("/work")
    async def work(request: Request):
        identifier = request.state.request_id
        await asyncio.sleep(0)
        assert request_id_context.get() == identifier
        observability.logger.info("operation completed", extra={"event": "warmer.test"})
        return {"request_id": identifier}

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            results = await asyncio.gather(
                *(
                    client.get("/work", headers={"x-request-id": f"id-{i}"})
                    for i in range(8)
                )
            )
            assert [r.json()["request_id"] for r in results] == [
                f"id-{i}" for i in range(8)
            ]
            assert [r.headers["x-request-id"] for r in results] == [
                f"id-{i}" for i in range(8)
            ]
        assert request_id_context.get() is None

    asyncio.run(run())
    assert len(logs()) == 16
    assert {item["request_id"] for item in logs()} == {f"id-{i}" for i in range(8)}


def test_cancellation_restores_context():
    async def run():
        request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})

        async def call_next(req):
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await log_requests(request, call_next)
        assert request_id_context.get() is None

    asyncio.run(run())
