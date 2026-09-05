"""Process-owned tracing to an explicitly configured private OTLP collector.

Configure before serving requests. Repeated calls share one provider/exporter;
shutdown the returned provider only at process shutdown, not per app/request.
HTTPX discovery is automatic. asyncio subprocess curl is intentionally not:
the warming operation owns explicit spans using trace.get_tracer(__name__).
Span names, log messages and allowed attributes must never contain payloads.
"""

import os
from threading import Lock
from urllib.parse import urlsplit, urlunsplit

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Link, Status

from app.observability import LOG_FIELDS, service_identity, valid_request_id

_lock = Lock()
_provider: TracerProvider | None = None
_SAFE_ATTRIBUTES = (
    frozenset(LOG_FIELDS)
    | {f"warmer.{field}" for field in LOG_FIELDS}
    | {
        "http.method",
        "http.request.method",
        "http.route",
        "http.status_code",
        "http.response.status_code",
        "http.flavor",
        "network.protocol.version",
        "server.address",
        "server.port",
        "net.peer.name",
        "net.peer.port",
        "error.type",
        "exception.type",
        "exception.escaped",
        "asgi.event.type",
    }
)
_URL_ATTRIBUTES = {"http.url", "url.full", "http.target", "url.path"}


def _safe_url(value: str) -> str:
    """Remove userinfo, query and fragment, including malformed URLs safely."""
    try:
        parts = urlsplit(value)
        if not parts.netloc:
            return parts.path
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "[REDACTED]"


def _redact_server_query(span, scope: dict[str, object]) -> None:
    if span is None or not span.is_recording():
        return
    # Do not construct URLs from untrusted Host/Forwarded headers or claim a
    # client origin from ASGI's potentially proxy-rewritten client address.
    path = _safe_url(str(scope.get("path", "")))
    for key in _URL_ATTRIBUTES:
        span.set_attribute(key, path)
    span.set_attribute("url.query", "[REDACTED]")


def _redact_httpx_query_sync(span, request) -> None:
    if span is None or not span.is_recording():
        return
    safe_url = _safe_url(str(request.url))
    span.set_attribute("http.url", safe_url)
    span.set_attribute("url.full", safe_url)
    span.set_attribute("url.query", "[REDACTED]")


async def _redact_httpx_query(span, request) -> None:
    _redact_httpx_query_sync(span, request)


def _safe_attributes(attributes) -> dict:
    result = {}
    for key, value in (attributes or {}).items():
        if key in _URL_ATTRIBUTES and isinstance(value, str):
            result[key] = _safe_url(value)
        elif key == "url.query":
            result[key] = "[REDACTED]"
        elif key in {"request_id", "warmer.request_id"}:
            if valid_request_id(value):
                result[key] = value
        elif key in _SAFE_ATTRIBUTES:
            result[key] = value
    return result


class _PrivacyExporter(SpanExporter):
    """Sanitize final snapshots, including attributes/events added after hooks.

    A new snapshot avoids mutating SDK spans shared with other processors.
    Only safe operational fields reach the network; raw headers, client origin,
    query bodies, exception text and status descriptions are never exported.
    """

    def __init__(self, exporter: SpanExporter) -> None:
        self.exporter = exporter

    def export(self, spans):
        sanitized = [
            ReadableSpan(
                name=span.name,
                context=span.context,
                parent=span.parent,
                resource=span.resource,
                attributes=_safe_attributes(span.attributes),
                events=[
                    Event(
                        "exception", _safe_attributes(event.attributes), event.timestamp
                    )
                    for event in span.events
                    if event.name == "exception"
                ],
                links=[
                    Link(link.context, _safe_attributes(link.attributes))
                    for link in span.links
                ],
                kind=span.kind,
                status=Status(span.status.status_code),
                start_time=span.start_time,
                end_time=span.end_time,
                instrumentation_scope=span.instrumentation_scope,
            )
            for span in spans
        ]
        return self.exporter.export(sanitized)

    def shutdown(self) -> None:
        self.exporter.shutdown()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return self.exporter.force_flush(timeout_millis)


def _make_exporter() -> SpanExporter:
    # Standard per-signal overrides take precedence. Remaining headers, TLS,
    # compression, timeout and retry settings are read by the exporter itself.
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "").strip()
    base_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    protocol = (
        os.getenv("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", "").strip()
        or os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL", "").strip()
        or "http/protobuf"
    )
    if protocol == "http/protobuf":
        endpoint = endpoint or f"{base_endpoint.rstrip('/')}/v1/traces"
        return OTLPSpanExporter(endpoint=endpoint)
    if protocol == "grpc":
        # Optional dependency; HTTP/protobuf follows the sibling services.
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as GRPCSpanExporter,
        )

        return GRPCSpanExporter(endpoint=endpoint or base_endpoint)
    raise ValueError("Unsupported OTLP trace protocol; use http/protobuf or grpc")


def configure_telemetry(app: FastAPI) -> TracerProvider | None:
    """Enable tracing only with an endpoint, respecting standard OTEL switches.

    This module owns process instrumentation. Do not combine it with a separate
    auto-instrumentation launcher/provider. Environment changes require restart.
    No AWS SDK, cloud discovery or credentials are used by the application.
    """
    global _provider
    if os.getenv("OTEL_SDK_DISABLED", "").strip().lower() == "true":
        return None
    exporters = os.getenv("OTEL_TRACES_EXPORTER", "otlp").strip().lower()
    if exporters == "none":
        return None
    if not any(
        os.getenv(key, "").strip()
        for key in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        )
    ):
        return None
    if exporters not in {"", "otlp"}:
        raise ValueError("Warmer supports OTEL_TRACES_EXPORTER=otlp or none")

    with _lock:
        existing = getattr(app.state, "telemetry_provider", None)
        if existing is not None:
            return existing
        if getattr(app, "_is_instrumented_by_opentelemetry", False):
            raise RuntimeError("FastAPI telemetry is already managed by another owner")
        instrumentor = HTTPXClientInstrumentor()
        if _provider is None:
            if not isinstance(trace.get_tracer_provider(), trace.ProxyTracerProvider):
                raise RuntimeError(
                    "The global tracer provider is already managed by another owner"
                )
            if instrumentor.is_instrumented_by_opentelemetry:
                raise RuntimeError(
                    "HTTPX telemetry is already managed by another owner"
                )
            exporter = _PrivacyExporter(_make_exporter())
            provider = TracerProvider(
                resource=Resource.create(
                    {
                        "service.version": app.version,
                        "deployment.environment.name": os.getenv(
                            "DEPLOYMENT_ENVIRONMENT", "development"
                        ),
                        **service_identity(),
                    }
                )
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)
            _provider = provider

        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=_provider,
            server_request_hook=_redact_server_query,
            http_capture_headers_server_request=[],
            http_capture_headers_server_response=[],
            http_capture_headers_sanitize_fields=[".*"],
            exclude_spans=["receive", "send"],
        )
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.instrument(
                tracer_provider=_provider,
                request_hook=_redact_httpx_query_sync,
                async_request_hook=_redact_httpx_query,
            )
        app.state.telemetry_provider = _provider
        return _provider
