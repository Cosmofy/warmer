import asyncio
import ipaddress
import json
import re
import time
from urllib.parse import urlsplit

from opentelemetry import propagate, trace

from app.config import Settings
from app.models import EdgeResult
from app.queries import Operation

tracer = trace.get_tracer(__name__)
MAX_STDOUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
_READ_CHUNK_BYTES = 64 * 1024
_CLEANUP_TIMEOUT = 2.0


class _OutputLimitExceeded(Exception):
    pass


async def _read_limited(stream: asyncio.StreamReader, limit: int, name: str) -> bytes:
    output = bytearray()
    while chunk := await stream.read(min(_READ_CHUNK_BYTES, limit - len(output) + 1)):
        if len(output) + len(chunk) > limit:
            raise _OutputLimitExceeded(f"{name}_limit_exceeded")
        output.extend(chunk)
    return bytes(output)


async def _write_input(process: asyncio.subprocess.Process, payload: bytes) -> None:
    try:
        process.stdin.write(payload)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # curl may reject the connection before consuming the request.
    finally:
        process.stdin.close()


async def _drain(stream: asyncio.StreamReader) -> None:
    while await stream.read(_READ_CHUNK_BYTES):
        pass  # Discard residual output without accumulating it in memory.


async def _cleanup_process(process: asyncio.subprocess.Process, tasks: list[asyncio.Task]) -> None:
    process.stdin.close()
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass  # The child can exit between the returncode check and kill.
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    try:
        async with asyncio.timeout(_CLEANUP_TIMEOUT):
            async with asyncio.TaskGroup() as group:
                group.create_task(_drain(process.stdout))
                group.create_task(_drain(process.stderr))
                group.create_task(process.wait())
    except TimeoutError:
        # An inherited pipe can outlive the killed child. Process exposes no
        # public pipe-close API; close its transport as a last-resort deadline.
        process._transport.close()


async def _communicate_bounded(process: asyncio.subprocess.Process, payload: bytes) -> bytes:
    tasks = [asyncio.create_task(_read_limited(process.stdout, MAX_STDOUT_BYTES, "stdout")),
             asyncio.create_task(_read_limited(process.stderr, MAX_STDERR_BYTES, "stderr")),
             asyncio.create_task(_write_input(process, payload))]
    try:
        output, _stderr, _ = await asyncio.gather(*tasks)
        await process.wait()
        return output
    finally:
        cleanup = asyncio.create_task(_cleanup_process(process, tasks))
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise asyncio.CancelledError


def allowed_ip(ip: str, ranges: list[str]) -> bool:
    try:
        address = ipaddress.ip_address(ip)
        return address.is_global and any(address in ipaddress.ip_network(net) for net in ranges)
    except ValueError:
        return False


def parse_response(raw: bytes, ip: str, operation: Operation, target_pop: str | None) -> EdgeResult:
    result = EdgeResult(ip=ip, target_pop=target_pop)
    # curl's final timing line is separate from body; never execute response data.
    message, marker, seconds = raw.rpartition(b"\nWARMER_TIME:")
    if not marker:
        result.error = "invalid_transport_output"
        return result
    try:
        result.duration_ms = round(float(seconds) * 1000, 3)
        header, _, body = message.partition(b"\r\n\r\n")
        # Ignore informational HTTP responses, e.g. 103 Early Hints.
        while header.splitlines()[0].split()[1].startswith(b"1"):
            header, _, body = body.partition(b"\r\n\r\n")
        result.status_code = int(header.splitlines()[0].split()[1])
        headers = {}
        for line in header.split(b"\r\n")[1:]:
            key, separator, value = line.partition(b":")
            if separator:
                headers[key.decode("ascii").lower()] = value.decode("latin-1").strip()
        result.server = headers.get("x-served-by", "")[:256] or None
        match = re.search(r"-([A-Z0-9]{3})$", (result.server or "").split(",")[-1].strip())
        result.actual_pop = match.group(1) if match else None
        result.cache_status = headers.get("gcdn-cache", "").upper()[:32] or None
        if result.status_code != 200:
            result.error = "http_error"
        elif not result.actual_pop:
            result.error = "missing_pop_header"
        elif target_pop and result.actual_pop != target_pop:
            result.error = "pop_mismatch"
        elif not operation.validate(json.loads(body)):
            result.error = "invalid_graphql_response"
        elif operation.name != "discovery" and result.cache_status not in {"MISS", "HIT"}:
            result.error = "uncacheable_or_stale_response"
        else:
            result.success = True
    except (ValueError, IndexError, UnicodeError):
        result.error = "invalid_response"
    return result


class EdgeClient:
    """Use curl's tested --resolve support without changing DNS or disabling TLS."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.hostname = urlsplit(settings.stellate_url).hostname

    async def request(self, ip: str, ranges: list[str], operation: Operation,
                      target_pop: str | None = None) -> EdgeResult:
        if not allowed_ip(ip, ranges):
            return EdgeResult(ip=ip, target_pop=target_pop, error="unsafe_ip")
        with tracer.start_as_current_span("warmer.edge_request") as span:
            if target_pop:
                span.set_attribute("warmer.target_pop", target_pop)
            span.set_attribute("warmer.query_name", operation.name)
            address = f"[{ip}]" if ":" in ip else ip
            args = ["curl", "-q", "--noproxy", "*", "--silent", "--show-error", "--proto", "=https",
                    "--connect-timeout", "5", "--max-time", str(self.settings.warmer_request_timeout),
                    "--max-filesize", "2097152", "--resolve", f"{self.hostname}:443:{address}",
                    self.settings.stellate_url, "-H", "Content-Type: application/json",
                    "-H", "gcdn-debug: 1", "--data-binary", "@-", "--dump-header", "-",
                    "--write-out", "\nWARMER_TIME:%{time_total}"]
            trace_headers: dict[str, str] = {}
            propagate.inject(trace_headers)
            # Trace baggage could contain secrets; forward only W3C trace headers.
            for name in ("traceparent", "tracestate"):
                if name in trace_headers and "\n" not in trace_headers[name] and "\r" not in trace_headers[name]:
                    args.extend(["-H", f"{name}: {trace_headers[name]}"])
            started = time.perf_counter()
            process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=_READ_CHUNK_BYTES)
            try:
                async with asyncio.timeout(self.settings.warmer_request_timeout + 2):
                    output = await _communicate_bounded(process, json.dumps({"query": operation.query}).encode())
                if process.returncode:
                    result = EdgeResult(ip=ip, target_pop=target_pop, error=f"curl_exit_{process.returncode}",
                                        duration_ms=round((time.perf_counter() - started) * 1000, 3))
                else:
                    result = parse_response(output, ip, operation, target_pop)
            except TimeoutError:
                result = EdgeResult(ip=ip, target_pop=target_pop, error="request_timeout")
            except _OutputLimitExceeded as error:
                result = EdgeResult(ip=ip, target_pop=target_pop, error=str(error))
            if result.actual_pop:
                span.set_attribute("warmer.actual_pop", result.actual_pop)
            if result.cache_status:
                span.set_attribute("warmer.cache_status", result.cache_status)
            span.set_attribute("warmer.duration_ms", result.duration_ms)
            span.set_attribute("warmer.outcome", "success" if result.success else result.error or "failed")
            return result
