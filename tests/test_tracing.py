import asyncio
import json
import socket
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, Request
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

from app import telemetry
from app.observability import JsonFormatter, log_requests


@pytest.fixture(autouse=True)
def isolated_telemetry(monkeypatch):
    # Never load main/lifespan/settings or inherit a real exporter/credential setup.
    import os

    for key in tuple(os.environ):
        if key.startswith("OTEL_"):
            monkeypatch.delenv(key, raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("network access is forbidden in tracing tests")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(telemetry, "_provider", None)
    current = {"provider": trace.ProxyTracerProvider()}
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: current["provider"])
    install = Mock(side_effect=lambda provider: current.update(provider=provider))
    monkeypatch.setattr(trace, "set_tracer_provider", install)
    exporter = InMemorySpanExporter()
    make_exporter = Mock(return_value=exporter)
    monkeypatch.setattr(telemetry, "_make_exporter", make_exporter)
    apps = []

    def configure():
        app = FastAPI(version="1.0.0")
        apps.append(app)
        return app, telemetry.configure_telemetry(app)

    yield SimpleNamespace(
        configure=configure,
        apps=apps,
        exporter=exporter,
        make_exporter=make_exporter,
        install=install,
    )
    for app in apps:
        if getattr(app, "_is_instrumented_by_opentelemetry", False):
            FastAPIInstrumentor.uninstrument_app(app)
    instrumentor = HTTPXClientInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    if telemetry._provider is not None:
        telemetry._provider.shutdown()


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "   "},
        {
            "OTEL_SDK_DISABLED": " TrUe ",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.invalid",
        },
        {
            "OTEL_TRACES_EXPORTER": "none",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector.invalid/v1/traces",
        },
    ],
)
def test_no_endpoint_or_disabled_has_no_global_side_effects(
    environment, monkeypatch, isolated_telemetry
):
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    app, provider = isolated_telemetry.configure()
    assert provider is None
    assert not getattr(app, "_is_instrumented_by_opentelemetry", False)
    assert not HTTPXClientInstrumentor().is_instrumented_by_opentelemetry
    isolated_telemetry.make_exporter.assert_not_called()
    isolated_telemetry.install.assert_not_called()


def test_repeated_apps_share_provider_exporter_and_httpx_wrapper(
    monkeypatch, isolated_telemetry
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    app, first = isolated_telemetry.configure()
    wrapper = vars(httpx.AsyncHTTPTransport)["handle_async_request"]
    assert telemetry.configure_telemetry(app) is first
    second_app, second = isolated_telemetry.configure()
    assert first is second is second_app.state.telemetry_provider
    assert vars(httpx.AsyncHTTPTransport)["handle_async_request"] is wrapper
    isolated_telemetry.make_exporter.assert_called_once()
    isolated_telemetry.install.assert_called_once_with(first)


def test_unconfigured_app_can_be_configured_later(monkeypatch, isolated_telemetry):
    app, provider = isolated_telemetry.configure()
    assert provider is None
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    provider = telemetry.configure_telemetry(app)
    assert provider is app.state.telemetry_provider
    isolated_telemetry.make_exporter.assert_called_once()


def test_provider_shutdown_flushes_pending_spans(monkeypatch, isolated_telemetry):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    exporter = isolated_telemetry.exporter
    shutdown = Mock(wraps=exporter.shutdown)
    monkeypatch.setattr(exporter, "shutdown", shutdown)
    _app, provider = isolated_telemetry.configure()
    with provider.get_tracer("test").start_as_current_span("warmer.run"):
        pass
    provider.shutdown()
    assert len(exporter.get_finished_spans()) == 1
    shutdown.assert_called_once()


def test_existing_global_provider_is_never_replaced(monkeypatch, isolated_telemetry):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    external = TracerProvider()
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: external)
    try:
        with pytest.raises(RuntimeError, match="global tracer provider"):
            isolated_telemetry.configure()
        isolated_telemetry.install.assert_not_called()
        isolated_telemetry.make_exporter.assert_not_called()
    finally:
        external.shutdown()


def test_existing_httpx_instrumentation_is_never_replaced(
    monkeypatch, isolated_telemetry
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    HTTPXClientInstrumentor().instrument(tracer_provider=trace.NoOpTracerProvider())
    with pytest.raises(RuntimeError, match="HTTPX telemetry"):
        isolated_telemetry.configure()
    isolated_telemetry.make_exporter.assert_not_called()


def test_resource_identity_and_standard_sampler(monkeypatch, isolated_telemetry):
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://collector.invalid/v1/traces"
    )
    monkeypatch.setenv("OTEL_SERVICE_NAME", "custom-warmer")
    monkeypatch.setenv("OTEL_NODE_NAME", "warmer-node-a")
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES",
        "service.instance.id=instance-a,deployment.environment.name=test",
    )
    monkeypatch.setenv("OTEL_TRACES_SAMPLER", "always_off")
    app, provider = isolated_telemetry.configure()
    attributes = provider.resource.attributes
    assert attributes["service.name"] == "custom-warmer"
    assert attributes["service.namespace"] == "cosmofy"
    assert attributes["service.instance.id"] == "instance-a"
    assert attributes["host.name"] == "warmer-node-a"
    assert attributes["service.version"] == app.version
    assert attributes["deployment.environment.name"] == "test"
    with provider.get_tracer("test").start_as_current_span("test") as span:
        assert not span.is_recording()
    provider.force_flush()
    assert not isolated_telemetry.exporter.get_finished_spans()


# Save the real factory before the autouse fixture replaces it with memory-only export.
MAKE_EXPORTER = telemetry._make_exporter


@pytest.mark.parametrize(
    "base,specific,expected",
    [
        (
            "http://collector.invalid:4318/base/",
            None,
            "http://collector.invalid:4318/base/v1/traces",
        ),
        (None, "http://collector.invalid/custom", "http://collector.invalid/custom"),
        (
            "http://other.invalid",
            "http://collector.invalid/custom",
            "http://collector.invalid/custom",
        ),
        ("http://collector.invalid", " ", "http://collector.invalid/v1/traces"),
    ],
)
def test_standard_http_endpoint_precedence(base, specific, expected, monkeypatch):
    if base is not None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", base)
    if specific is not None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", specific)
    factory = Mock()
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", factory)
    assert MAKE_EXPORTER() is factory.return_value
    factory.assert_called_once_with(endpoint=expected)


def test_signal_protocol_overrides_generic_and_invalid_protocol_is_rejected(
    monkeypatch,
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/protobuf")
    factory = Mock()
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", factory)
    MAKE_EXPORTER()
    factory.assert_called_once()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "http/json")
    with pytest.raises(ValueError, match="Unsupported OTLP trace protocol"):
        MAKE_EXPORTER()


def test_optional_grpc_uses_standard_endpoint_without_http_suffix(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid:4317")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    factory = Mock()
    monkeypatch.setitem(
        sys.modules,
        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter",
        SimpleNamespace(OTLPSpanExporter=factory),
    )
    assert MAKE_EXPORTER() is factory.return_value
    factory.assert_called_once_with(endpoint="http://collector.invalid:4317")


def test_server_and_both_httpx_hooks_strip_sensitive_url_components():
    span = Mock()
    telemetry._redact_server_query(
        span,
        {
            "path": "/warm/articles",
            "headers": [(b"host", b"spoofed")],
            "query_string": b"query=private",
            "client": ("203.0.113.1", 1),
        },
    )
    attributes = dict(call.args for call in span.set_attribute.call_args_list)
    assert attributes["http.url"] == "/warm/articles"
    assert "spoofed" not in repr(attributes)
    request = SimpleNamespace(
        url=httpx.URL(
            "https://user:secret@discovery.invalid/pops?token=private#fragment"
        )
    )
    for hook in (telemetry._redact_httpx_query_sync, telemetry._redact_httpx_query):
        span.reset_mock()
        result = hook(span, request)
        if result is not None:
            asyncio.run(result)
        attributes = dict(call.args for call in span.set_attribute.call_args_list)
        assert attributes["http.url"] == "https://discovery.invalid/pops"
        assert attributes["url.full"] == "https://discovery.invalid/pops"
        assert attributes["url.query"] == "[REDACTED]"
    span.reset_mock()
    span.is_recording.return_value = False
    telemetry._redact_server_query(span, {})
    asyncio.run(telemetry._redact_httpx_query(span, request))
    span.set_attribute.assert_not_called()


def test_w3c_server_discovery_and_explicit_curl_span_chain(
    monkeypatch, isolated_telemetry
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", ".*")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_REQUEST", ".*")
    monkeypatch.setenv(
        "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_CLIENT_RESPONSE", ".*"
    )
    outgoing = []

    async def fake_send(self, request):
        outgoing.append(request)
        return httpx.Response(
            200, json={"pops": []}, headers={"set-cookie": "private-cookie"}
        )

    # Patch below the automatic wrapper, so no socket can be opened.
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fake_send)
    app, provider = isolated_telemetry.configure()
    app.middleware("http")(log_requests)
    captured_log = []

    @app.post("/warm/articles")
    async def warm(request: Request):
        import logging

        record = logging.LogRecord("app.test", 20, __file__, 1, "run started", (), None)
        captured_log.append(json.loads(JsonFormatter().format(record)))
        async with httpx.AsyncClient(trust_env=False) as client:
            await client.get(
                "https://discovery.invalid/pops?query=private-query",
                headers={"authorization": "Bearer private-token"},
            )
        with trace.get_tracer("app.warming").start_as_current_span(
            "warmer.curl", kind=SpanKind.CLIENT
        ) as span:
            span.set_attribute("warmer.run_id", "run-1")
            span.set_attribute("warmer.target_pop", "LHR")
            span.set_attribute("warmer.actual_pop", "YYZ")
            span.set_attribute("warmer.coverage", 0.5)
        return {"request_id": request.state.request_id}

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/warm/articles?query=private-query",
                json={"query": "private-body"},
                headers={
                    "traceparent": "00-11111111111111111111111111111111-2222222222222222-01",
                    "tracestate": "cosmofy=test",
                    "x-request-id": "edge-1",
                    "authorization": "Bearer private-token",
                    "x-forwarded-for": "203.0.113.77",
                    "cf-ipcountry": "GB",
                },
            )

    response = asyncio.run(run())
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "edge-1"
    assert provider.force_flush()
    spans = isolated_telemetry.exporter.get_finished_spans()
    assert len(spans) == 3
    server = next(span for span in spans if span.kind == SpanKind.SERVER)
    children = [span for span in spans if span.kind == SpanKind.CLIENT]
    assert server.parent.span_id == int("2222222222222222", 16)
    assert all(span.context.trace_id == int("1" * 32, 16) for span in spans)
    assert all(span.parent.span_id == server.context.span_id for span in children)
    assert outgoing[0].headers["traceparent"].split("-")[1] == "1" * 32
    assert outgoing[0].headers["tracestate"] == "cosmofy=test"
    assert captured_log[0]["trace_id"] == "1" * 32
    assert captured_log[0]["span_id"] == format(server.context.span_id, "016x")
    assert captured_log[0]["request_id"] == "edge-1"
    assert server.attributes["request_id"] == "edge-1"
    curl = next(span for span in children if span.name == "warmer.curl")
    assert curl.attributes["warmer.target_pop"] == "LHR"
    serialized = " ".join(span.to_json() for span in spans)
    for secret in (
        "private-query",
        "private-body",
        "private-token",
        "private-cookie",
        "203.0.113.77",
        "http.request.header",
    ):
        assert secret not in serialized
    assert "client.address" not in serialized


def test_failed_httpx_exception_and_late_attributes_are_sanitized(
    monkeypatch, isolated_telemetry
):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector.invalid")

    def fail_send(self, request):
        raise httpx.ConnectError(
            "private-token query { private-body }", request=request
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail_send)
    _app, provider = isolated_telemetry.configure()
    with httpx.Client(trust_env=False) as client, pytest.raises(httpx.ConnectError):
        client.get("https://user:password@discovery.invalid/pops?query=private-query")
    with provider.get_tracer("app.test").start_as_current_span("warmer.run") as span:
        span.set_attribute("graphql.document", "private-body")
        span.set_attribute("http.request.header.authorization", "private-token")
        span.set_attribute("client.address", "spoofed-origin")
        span.set_attribute("request_id", "bad\nid")
        span.set_attribute("run_id", "run-1")
        span.set_attribute("count", 2)
        span.set_attribute("duration_ms", 12.5)
        span.set_status(Status(StatusCode.ERROR, "private-token"))
        span.add_event("private-body", {"query": "private-body"})
    assert provider.force_flush()
    spans = isolated_telemetry.exporter.get_finished_spans()
    assert len(spans) == 2
    assert spans[0].status.status_code == StatusCode.ERROR
    assert spans[0].events[0].attributes["exception.type"] == "httpx.ConnectError"
    assert spans[1].attributes["run_id"] == "run-1"
    assert spans[1].attributes["count"] == 2
    assert spans[1].attributes["duration_ms"] == 12.5
    serialized = " ".join(span.to_json() for span in spans)
    for secret in (
        "private-token",
        "private-body",
        "private-query",
        "password",
        "spoofed-origin",
        "bad\\nid",
    ):
        assert secret not in serialized
    assert spans[0].status.description is None
    assert spans[1].status.description is None
    # Exercise the real OTLP protobuf encoder without contacting a collector.
    wire_data = encode_spans(spans).SerializeToString()
    assert wire_data
    assert b"warmer.run" in wire_data
    assert b"private-token" not in wire_data
    assert b"private-body" not in wire_data
