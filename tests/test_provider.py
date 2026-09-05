import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.config import Settings
from app import provider
from app.provider import EdgeClient, allowed_ip, parse_response
from app.queries import PROBE, resolve_operation


def response(body=None, cache="MISS", pop="BOM", status=200):
    if body is None:
        body = {"data": {"articles": [{"id": "one"}]}}
    return (f"HTTP/2 {status}\r\nx-served-by: cache-test-{pop}\r\ngcdn-cache: {cache}\r\n\r\n"
            + json.dumps(body) + "\nWARMER_TIME:0.125").encode()


@pytest.mark.parametrize("cache", ["MISS", "HIT"])
def test_valid_cacheable_response(cache):
    r = parse_response(response(cache=cache), "151.101.209.51", resolve_operation("articles"), "BOM")
    assert r.success and r.actual_pop == "BOM" and r.duration_ms == 125


@pytest.mark.parametrize("cache", ["PASS", "STALE", "PARTIAL", "", "BYPASS"])
def test_uncacheable_response_is_not_warmed(cache):
    r = parse_response(response(cache=cache), "151.101.209.51", resolve_operation("articles"), "BOM")
    assert not r.success


def test_mismatched_pop_does_not_count_for_target():
    r = parse_response(response(pop="LHR"), "151.101.209.51", resolve_operation("articles"), "BOM")
    assert r.error == "pop_mismatch" and r.actual_pop == "LHR"


@pytest.mark.parametrize("body", [{"errors": [{"message": "broken"}], "data": {"articles": []}},
                                   {"data": None}, {"data": {"articles": None}},
                                   {"data": {"articles": [{"id": None}]}}, {}])
def test_http_200_does_not_hide_graphql_failures(body):
    assert not parse_response(response(body), "151.101.209.51", resolve_operation("articles"), "BOM").success


def test_discovery_can_verify_uncacheable_typename():
    r = parse_response(response({"data": {"__typename": "Query"}}, cache="PASS"), "151.101.209.51", PROBE, None)
    assert r.success


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "100.90.253.79", "8.8.8.8", "garbage"])
def test_only_public_fastly_addresses_allowed(ip):
    assert not allowed_ip(ip, ["151.101.0.0/16"])


def test_direct_address_must_be_in_published_ranges():
    assert allowed_ip("151.101.209.51", ["151.101.0.0/16"])
    assert not allowed_ip("151.101.209.51", [])


def test_malformed_transport_response():
    assert not parse_response(b"invalid", "151.101.209.51", PROBE, None).success


def test_transport_keeps_hostname_tls_and_no_shell(monkeypatch):
    settings = Settings(warmer_api_token="x" * 32, stellate_url="https://livia.stellate.sh")
    async def scenario():
        stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
        stdout.feed_data(response())
        stdout.feed_eof()
        stderr.feed_eof()
        process = SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr,
                                  stdin=Mock(drain=AsyncMock()), wait=AsyncMock(return_value=0), kill=Mock())
        create = AsyncMock(return_value=process)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
        result = await EdgeClient(settings).request("151.101.209.51", ["151.101.0.0/16"], resolve_operation("articles"), "BOM")
        process.kill.assert_not_called()
        assert json.loads(process.stdin.write.call_args.args[0]) == {"query": resolve_operation("articles").query}
        return result, create
    r, create = asyncio.run(scenario())
    assert r.success
    args = create.call_args.args
    assert args[:2] == ("curl", "-q")
    assert "livia.stellate.sh:443:151.101.209.51" in args
    assert "https://livia.stellate.sh" in args
    assert "--insecure" not in args and "-k" not in args and "-L" not in args
    assert "--data-binary" in args and "@-" in args
    assert "shell" not in create.call_args.kwargs


def test_no_subprocess_for_unsafe_address(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    r = asyncio.run(EdgeClient(Settings(warmer_api_token="x" * 32)).request("127.0.0.1", ["0.0.0.0/0"], PROBE))
    assert r.error == "unsafe_ip"
    create.assert_not_called()


@pytest.mark.parametrize("name", ["stdout", "stderr"])
@pytest.mark.parametrize("extra", [0, 1])
def test_stream_caps_accept_exact_limit_and_reject_one_extra_byte(name, extra):
    async def scenario():
        stream = asyncio.StreamReader()
        stream.feed_data(b"x" * (128 + extra))
        stream.feed_eof()
        if extra:
            with pytest.raises(provider._OutputLimitExceeded, match=f"{name}_limit_exceeded"):
                await provider._read_limited(stream, 128, name)
        else:
            assert await provider._read_limited(stream, 128, name) == b"x" * 128
    asyncio.run(scenario())


def fake_child(monkeypatch, script, setup=None):
    """Replace curl with a real pipe-writing Python child, without shell or network."""
    children = []
    create = asyncio.create_subprocess_exec
    async def spawn(*_args, **kwargs):
        child = await create(sys.executable, "-B", "-c", script, **kwargs)
        children.append(child)
        if setup:
            setup(child)
        return child
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return children


def assert_child_closed(child):
    assert child.returncode is not None
    assert child.stdin.is_closing()
    assert child.stdout.at_eof()
    assert child.stderr.at_eof()
    assert child._transport.get_pipe_transport(1).is_closing()
    assert child._transport.get_pipe_transport(2).is_closing()


async def stop_children(children):
    # Test-failure safety net; assertions above must pass before this cleanup.
    for child in children:
        if child.returncode is None:
            try:
                child.kill()
            except ProcessLookupError:
                pass
        child._transport.close()
    async with asyncio.timeout(2):
        await asyncio.gather(*(child.wait() for child in children))


@pytest.mark.parametrize("name,fd,limit", [("stdout", 1, provider.MAX_STDOUT_BYTES),
                                         ("stderr", 2, provider.MAX_STDERR_BYTES)])
def test_real_child_output_caps_kill_and_reap_without_curl_support(monkeypatch, name, fd, limit):
    async def scenario():
        children = fake_child(monkeypatch, f"""
import os, sys, time
sys.stdin.buffer.read()
for _ in range({limit // 4096 + 1}):
    os.write({fd}, b'x' * 4096)
time.sleep(30)
""")
        try:
            result = await asyncio.wait_for(EdgeClient(Settings(warmer_api_token="x" * 32)).request(
                "151.101.209.51", ["151.101.0.0/16"], PROBE), 5)
            assert not result.success and result.error == f"{name}_limit_exceeded"
            assert_child_closed(children[0])
        finally:
            await stop_children(children)
    asyncio.run(scenario())


def test_real_child_reads_both_pipes_and_handles_early_stdin_close(monkeypatch):
    async def scenario():
        children = fake_child(monkeypatch, f"""
import os
os.close(0)
os.write(2, b'e' * {provider.MAX_STDERR_BYTES})
os.write(1, {response()!r})
""")
        try:
            result = await asyncio.wait_for(EdgeClient(Settings(warmer_api_token="x" * 32)).request(
                "151.101.209.51", ["151.101.0.0/16"], resolve_operation("articles"), "BOM"), 5)
            assert result.success
            assert_child_closed(children[0])
        finally:
            await stop_children(children)
    asyncio.run(scenario())


@pytest.mark.parametrize("repeat_cancel", [False, True])
def test_real_child_cancellation_drains_paused_pipe_and_reaps(monkeypatch, repeat_cancel):
    async def scenario():
        spawned, release_reader = asyncio.Event(), asyncio.Event()
        cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
        cleanup = provider._cleanup_process
        async def delayed_cleanup(process, tasks):
            cleanup_started.set()
            await release_cleanup.wait()
            await cleanup(process, tasks)
        monkeypatch.setattr(provider, "_cleanup_process", delayed_cleanup)
        def setup(child):
            read = child.stdout.read
            async def delayed_read(size):
                await release_reader.wait()
                return await read(size)
            child.stdout.read = delayed_read
            spawned.set()
        children = fake_child(monkeypatch, """
import os, sys, time
sys.stdin.buffer.read()
for _ in range(512):
    os.write(1, b'x' * 4096)
time.sleep(30)
""", setup)
        task = asyncio.create_task(EdgeClient(Settings(warmer_api_token="x" * 32)).request(
            "151.101.209.51", ["151.101.0.0/16"], PROBE))
        try:
            async with asyncio.timeout(5):
                await spawned.wait()
                while not children[0].stdout._paused:
                    await asyncio.sleep(0.001)
                # The real pipe is backed up beyond StreamReader's high-water mark.
                assert len(children[0].stdout._buffer) > 2 * provider._READ_CHUNK_BYTES
                task.cancel()
                await cleanup_started.wait()
                if repeat_cancel:
                    task.cancel()
                    await asyncio.sleep(0)
                release_reader.set()
                release_cleanup.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert_child_closed(children[0])
            assert not [pending for pending in asyncio.all_tasks() if pending is not asyncio.current_task()]
        finally:
            release_reader.set()
            release_cleanup.set()
            task.cancel()
            await stop_children(children)
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_real_child_deadline_kills_drains_and_reaps(monkeypatch):
    async def scenario():
        children = fake_child(monkeypatch, "import sys, time; sys.stdin.buffer.read(); time.sleep(30)")
        try:
            result = await asyncio.wait_for(EdgeClient(Settings(
                warmer_api_token="x" * 32, warmer_request_timeout=1)).request(
                    "151.101.209.51", ["151.101.0.0/16"], PROBE), 5)
            assert result.error == "request_timeout"
            assert_child_closed(children[0])
        finally:
            await stop_children(children)
    asyncio.run(scenario())
