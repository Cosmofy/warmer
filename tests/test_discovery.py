import asyncio
import json
import socket
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock

import httpx
import pytest

from app import discovery
from app.config import Settings
from app.discovery import Discovery, DiscoveryError, parse_pops, parse_ranges
from app.models import EdgeResult, Inventory, Mapping, Pop
from app.queries import PROBE

RANGES = {"addresses": ["151.101.0.0/16"], "ipv6_addresses": ["2a04:4e42::/32"]}
IP = "151.101.1.1"
IP2 = "151.101.2.1"
OLD = datetime(2025, 1, 1, tzinfo=UTC)
LOCAL_CANDIDATES = Discovery._local_candidates


def html(pops=(("AMS", "Amsterdam"),)):
    rows = "".join(f"<tr><td>{city}</td><td><strong>{code}</strong></td><td>1, 2</td></tr>"
                   for code, city in pops)
    return (
        '<a href="#complete-list-of-pops">Complete list of POPs</a>'
        '<table><tr><td>OLD</td></tr></table>'
        '<h2 id="complete-list-of-pops"><a href="#complete-list-of-pops"></a>Complete list of POPs</h2>'
        '<p>All active POPs.</p><div><table><tr><th>Location</th><th>POP Identifier</th>'
        f'<th>Approx location</th></tr>{rows}</table></div>'
        '<footer><table><tr><th>Location</th><th>POP Identifier</th></tr>'
        '<tr><td>Not a target</td><td>BAD</td></tr></table></footer>'
    )


def markdown(pops=(("AMS", "Amsterdam"),)):
    rows = "\n".join(f"| {city} | **{code}** | 1, 2 |" for code, city in pops)
    return (
        '[Complete list of POPs](#complete-list-of-pops)\n\n'
        '| Old table | Code |\n| --- | --- |\n| Never use | OLD |\n\n'
        '## Complete list of POPs\n\nAll active POPs.\n\n'
        '| Location | POP Identifier | Approx location |\n'
        '| :--- | ---: | --- |\n' + rows + '\n\n'
        '## Footer\n\n| Location | POP Identifier |\n| --- | --- |\n| Footer | BAD |\n'
    )


def settings(**overrides):
    overrides.setdefault("stellate_url", "https://livia.stellate.sh")
    return Settings(_env_file=None, warmer_api_token="x" * 32, **overrides)


def test_metro_only_markdown_cannot_bootstrap_a_partial_target_list():
    document = markdown().replace("Approx location", "Sites spanned")
    with pytest.raises(DiscoveryError, match="inventory_invalid"):
        parse_pops(document)


def previous(pops=(("AMS", "Amsterdam"),), ips=(("AMS", IP),)):
    return Inventory(fetched_at=OLD, source="old", pops=[Pop(code=code, city=city) for code, city in pops],
                     fastly_ranges=RANGES["addresses"],
                     mappings=[Mapping(pop=pop, ip=ip, server="old-server", verified_at=OLD) for pop, ip in ips],
                     discovery_errors=["old_failure"])


def edge_result(ip=IP, pop="AMS", **overrides):
    values = dict(ip=ip, actual_pop=pop, server=f"cache-test-{pop}", status_code=200, success=True)
    values.update(overrides)
    return EdgeResult(**values)


def dns_result(city="Amsterdam", ips=(IP,), **overrides):
    result = {"status": "finished", "statusCode": 0,
              "answers": [{"type": "AAAA" if ":" in ip else "A", "value": ip} for ip in ips]}
    result.update(overrides)
    return {"probe": {"city": city}, "result": result}


def measurement(results=None, status="finished"):
    return {"type": "dns", "target": "livia.stellate.sh", "status": status,
            "results": [dns_result()] if results is None else results}


class Provider:
    """All requests use MockTransport; no network, subprocesses, or credits."""

    def __init__(self, document=None, cities=("Amsterdam",), results=None):
        self.document = html() if document is None else document
        self.cities = cities
        self.results = measurement() if results is None else results
        self.requests = []
        self.override = None

    def __call__(self, request):
        self.requests.append(request)
        if self.override:
            response = self.override(request)
            if response is not None:
                return response
        if str(request.url) == discovery.POPS_URL:
            # Content type is deliberately untrustworthy for markdown fixtures.
            return httpx.Response(200, text=self.document, headers={"Content-Type": "text/html"})
        if str(request.url) == discovery.RANGES_URL:
            return httpx.Response(200, json=RANGES)
        if request.url.path == "/v1/probes":
            return httpx.Response(200, json=[{"location": {"city": city}} for city in self.cities])
        if request.method == "POST" and request.url.path == "/v1/measurements":
            return httpx.Response(202, json={"id": "test-id", "probesCount": 3})
        if request.url.path == "/v1/measurements/test-id":
            return httpx.Response(200, json=self.results)
        raise AssertionError(f"Unexpected mocked request: {request.method} {request.url.path}")


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch):
    monkeypatch.setattr(discovery, "API_INTERVAL", 0)
    monkeypatch.setattr(discovery, "POLL_INTERVAL", 0)
    # Ordinary discovery tests must never ask the real local DNS resolver.
    monkeypatch.setattr(Discovery, "_local_candidates", AsyncMock(return_value=set()))


def refresh(provider, old=None, edge=None, config=None, **client_options):
    edge = edge or AsyncMock()
    if edge.request.return_value is None or not isinstance(edge.request.return_value, EdgeResult):
        edge.request.return_value = edge_result()

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider), **client_options) as client:
            return await Discovery(client, edge, config or settings()).refresh(old)

    return asyncio.run(run()), edge


@pytest.mark.parametrize("document", [html, markdown])
def test_parser_full_table_keeps_every_code_and_excludes_footer(document):
    pops = [(f"{number:03X}", "Shared city") for number in range(180)]
    parsed = parse_pops(document(pops))
    assert {pop.code for pop in parsed} == {code for code, _ in pops}
    assert len(parsed) == 180


def test_parser_normalizes_markup_and_entities():
    assert parse_pops(html([("SJC", "San&nbsp;Jose &amp; Bay")])) == [Pop(code="SJC", city="San Jose & Bay")]
    assert parse_pops(markdown([("AMS", "[Amsterdam](https://example.invalid)")])) == [Pop(code="AMS", city="Amsterdam")]


@pytest.mark.parametrize("document", [
    "<h2>Complete list of POPs</h2><p>Unavailable</p>",
    "## Complete list of POPs\n\nNo table\n",
    html().replace("<strong>AMS</strong>", "<strong>LONGCODE</strong>"),
    html().replace("<td>1, 2</td>", ""),
    html().split("</table></div>")[0],
    html().replace("</table></div>", "<tr><td>Delhi</td><td>DEL</td></table></div>"),
    html().replace("<td>1, 2</td>", "<td>1, 2"),
    html([]),
    markdown([]),
    html().replace("<th>POP Identifier</th>", "<th>Wrong</th>"),
    html([("AMS", "Amsterdam"), ("AMS", "Another city")]),
    '<h2>Complete list of POPs</h2><h2>Footer</h2><table><tr><th>Location</th><th>POP Identifier</th></tr><tr><td>Footer</td><td>BAD</td></tr></table>',
])
def test_partial_or_invalid_inventory_fails_closed(document):
    with pytest.raises(DiscoveryError, match="^inventory_invalid$"):
        parse_pops(document)


def test_markdown_first_table_only_even_without_next_heading():
    doc = markdown().replace("## Footer", "Footer")
    assert [pop.code for pop in parse_pops(doc)] == ["AMS"]


@pytest.mark.parametrize("payload", [
    None, [], {}, {"addresses": [], "ipv6_addresses": []},
    {"addresses": "151.101.0.0/16", "ipv6_addresses": []},
    *({"addresses": [net], "ipv6_addresses": []} for net in (
        "127.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "0.0.0.0/0",
        "8.0.0.0/6", "224.0.0.0/4", "151.101.1.1/16", "nonsense", 42,
    )),
    {"addresses": [], "ipv6_addresses": ["::/0"]},
    {"addresses": [], "ipv6_addresses": ["2000::/3"]},
    {"addresses": ["2a04:4e42::/32"], "ipv6_addresses": []},
])
def test_public_ranges_must_be_valid_global_cidrs(payload):
    with pytest.raises(DiscoveryError, match="public_ranges_invalid"):
        parse_ranges(payload)


def test_ranges_are_normalized_and_deduplicated():
    assert parse_ranges({"addresses": RANGES["addresses"] * 2, "ipv6_addresses": RANGES["ipv6_addresses"]}) == [
        "151.101.0.0/16", "2a04:4e42::/32",
    ]


@pytest.mark.parametrize("source", [html, markdown])
def test_refresh_success_uses_only_server_owned_probe(source):
    provider = Provider(source())
    inventory, edge = refresh(provider)
    assert inventory.discovery_errors == []
    assert inventory.source == discovery.POPS_URL
    assert inventory.fetched_at > OLD
    assert len(inventory.mappings) == 1
    assert inventory.mappings[0].verified_at > OLD
    edge.request.assert_awaited_once_with(IP, parse_ranges(RANGES), PROBE)
    payload = json.loads(next(request.content for request in provider.requests if request.method == "POST"))
    assert payload == {"type": "dns", "target": "livia.stellate.sh",
                       "locations": [{"city": "Amsterdam", "limit": 3}]}
    assert "limit" not in payload


def test_disappearing_targets_remain_and_new_city_metadata_wins():
    old = previous(pops=(("AMS", "Old Amsterdam"), ("RTM", "Amsterdam")))
    inventory, _ = refresh(Provider(), old)
    assert [(pop.code, pop.city) for pop in inventory.pops] == [("AMS", "Amsterdam"), ("RTM", "Amsterdam")]
    assert "inventory_targets_absent:RTM" in inventory.discovery_errors
    assert "missing_mappings:RTM" in inventory.discovery_errors
    assert "old_failure" not in inventory.discovery_errors
    assert old.pops[0].city == "Old Amsterdam"
    assert old.mappings[0].verified_at == OLD


def test_request_prefers_full_html_over_incomplete_negotiated_markdown():
    provider = Provider(html([("AMS", "Amsterdam"), ("RTM", "Amsterdam")]))

    def negotiated_response(request):
        if str(request.url) == discovery.POPS_URL and "text/markdown" in request.headers.get("Accept", ""):
            # Reproduces the live source incompatibility: its negotiated markdown
            # is a metro-sites subset, not the full HTML POP inventory.
            return httpx.Response(200, text=markdown().replace("Approx location", "Sites spanned"),
                                  headers={"Content-Type": "text/markdown"})

    provider.override = negotiated_response
    inventory, _ = refresh(provider)
    assert {pop.code for pop in inventory.pops} == {"AMS", "RTM"}
    assert provider.requests[0].headers["Accept"] == "text/html"
    assert "missing_mappings:RTM" in inventory.discovery_errors


def test_previous_candidates_reverified_and_regrouped_by_actual_pop():
    provider = Provider(html([("AMS", "Amsterdam"), ("RTM", "Amsterdam")]),
                        results=measurement([dns_result(ips=(IP, IP2))]))
    edge = AsyncMock()
    edge.request.side_effect = lambda ip, ranges, operation: edge_result(ip, "RTM" if ip == IP else "AMS")
    inventory, edge = refresh(provider, previous(), edge)
    assert [(mapping.pop, mapping.ip) for mapping in inventory.mappings] == [("AMS", IP2), ("RTM", IP)]
    assert all(mapping.verified_at > OLD and mapping.server != "old-server" for mapping in inventory.mappings)
    assert edge.request.await_count == 2  # Previous and DNS duplicates are probed once this refresh.
    assert len([request for request in provider.requests if request.method == "POST"]) == 1
    assert inventory.discovery_errors == []


@pytest.mark.parametrize("ips", [("127.0.0.1",), ("10.1.1.1",), ("169.254.169.254",),
                                 ("100.64.0.1",), ("8.8.8.8",), ("224.0.0.1",),
                                 ("::1",), ("fe80::1",), ("2a04:4e42::1%eth0",), ("not-an-ip",)])
def test_dns_and_previous_unsafe_candidates_never_reach_edge(ips):
    provider = Provider(results=measurement([dns_result(ips=ips)]))
    inventory, edge = refresh(provider, previous(ips=(("AMS", ips[0]),)))
    edge.request.assert_not_awaited()
    assert inventory.mappings == []
    assert "unsafe_candidate" in inventory.discovery_errors
    assert "globalping_unsafe_answer" in inventory.discovery_errors


def test_safe_ipv6_and_cname_ignored_and_no_raw_output_ip_scraping():
    item = dns_result(ips=("2a04:4e42::1",))
    item["result"]["answers"].append({"type": "CNAME", "value": "151.101.9.1"})
    item["result"]["rawOutput"] = "151.101.8.1"
    provider = Provider(results=measurement([item]))
    edge = AsyncMock()
    edge.request.side_effect = lambda ip, ranges, operation: edge_result(ip)
    inventory, edge = refresh(provider, edge=edge)
    assert [mapping.ip for mapping in inventory.mappings] == ["2a04:4e42::1"]
    assert edge.request.await_count == 1


@pytest.mark.parametrize("result", [edge_result(pop="ZZZ"), edge_result(success=False, error="secret upstream detail"),
                                    edge_result(status_code=503), edge_result(server=None)])
def test_unknown_pop_and_failed_graphql_never_become_mappings(result):
    edge = AsyncMock()
    edge.request.return_value = result
    inventory, _ = refresh(Provider(), previous(), edge)
    assert inventory.mappings == []
    assert "missing_mappings:AMS" in inventory.discovery_errors
    assert "secret" not in inventory.model_dump_json()


def test_missing_city_is_skipped_without_losing_target_denominator():
    provider = Provider(html([("AMS", "Amsterdam"), ("DEL", "Delhi"), ("RTM", "Amsterdam")]))
    inventory, _ = refresh(provider)
    assert {pop.code for pop in inventory.pops} == {"AMS", "DEL", "RTM"}
    assert "globalping_city_unavailable:DEL" in inventory.discovery_errors
    assert "missing_mappings:DEL,RTM" in inventory.discovery_errors
    posted = [json.loads(request.content) for request in provider.requests if request.method == "POST"]
    assert len(posted) == 1
    assert posted[0]["locations"] == [{"city": "Amsterdam", "limit": 3}]


def test_city_disappearing_after_probe_listing_does_not_fail_other_cities():
    provider = Provider(html([("AMS", "Amsterdam"), ("DEL", "Delhi")]), cities=("Amsterdam", "Delhi"))

    def reject(request):
        if request.method == "POST" and any(location["city"] == "Delhi" for location in json.loads(request.content)["locations"]):
            return httpx.Response(422, json={"error": {"message": "private upstream details"}})

    provider.override = reject
    inventory, _ = refresh(provider)
    assert [mapping.pop for mapping in inventory.mappings] == ["AMS"]
    assert "globalping_locations_unavailable" in inventory.discovery_errors
    assert "missing_mappings:DEL" in inventory.discovery_errors
    assert len([request for request in provider.requests if request.method == "POST"]) == 3


@pytest.mark.parametrize("status", [401, 403, 429, 503])
def test_dns_outage_returns_initial_full_inventory_and_safe_errors(status, monkeypatch):
    monkeypatch.setattr(discovery, "REQUEST_ATTEMPTS", 1)
    provider = Provider(html([("AMS", "Amsterdam"), ("RTM", "Amsterdam")]))
    provider.override = lambda request: httpx.Response(status, text="private-token", headers={"Retry-After": "999999"}) if request.url.host == "api.globalping.io" else None
    inventory, edge = refresh(provider, config=settings(globalping_token="private-token"))
    assert len(inventory.pops) == 2
    assert inventory.discovery_errors
    assert inventory.mappings == []
    edge.request.assert_not_awaited()
    assert "private-token" not in inventory.model_dump_json()


def test_full_fresh_previous_coverage_needs_no_globalping_even_during_outage(monkeypatch):
    monkeypatch.setattr(discovery, "REQUEST_ATTEMPTS", 1)
    provider = Provider()
    provider.override = lambda request: httpx.Response(503) if request.url.host == "api.globalping.io" else None
    inventory, edge = refresh(provider, previous())
    assert [mapping.pop for mapping in inventory.mappings] == ["AMS"]
    assert inventory.mappings[0].verified_at > OLD
    assert inventory.discovery_errors == []
    assert all(request.url.host != "api.globalping.io" for request in provider.requests)
    edge.request.assert_awaited_once()


def test_advisory_probes_failure_still_tries_regional_dns():
    provider = Provider()
    provider.override = lambda request: httpx.Response(404) if request.url.path == "/v1/probes" else None
    inventory, _ = refresh(provider)
    assert [mapping.pop for mapping in inventory.mappings] == ["AMS"]
    assert "globalping_http_error" in inventory.discovery_errors


@pytest.mark.parametrize("token", [None, "test-secret-token"])
def test_bearer_only_goes_to_api_and_shared_client_credentials_are_removed(token, caplog):
    provider = Provider()
    inventory, _ = refresh(provider, config=settings(globalping_token=token),
                           headers={"Authorization": "other-secret", "Cookie": "private-cookie"},
                           auth=("private-user", "private-password"))
    assert inventory.discovery_errors == []
    for request in provider.requests:
        expected = f"Bearer {token}" if token and request.url.host == "api.globalping.io" else None
        assert request.headers.get("Authorization") == expected
        assert "Cookie" not in request.headers
    assert "test-secret-token" not in caplog.text
    assert "other-secret" not in caplog.text


def test_429_retries_only_after_shared_cooldown(monkeypatch):
    provider = Provider()
    posts = 0

    def rate_limit(request):
        nonlocal posts
        if request.method == "POST":
            posts += 1
            if posts == 1:
                return httpx.Response(429, headers={"Retry-After": "12"})

    provider.override = rate_limit
    waits = AsyncMock()
    delays = []
    monkeypatch.setattr(discovery._ApiPacer, "wait", waits)
    monkeypatch.setattr(discovery._ApiPacer, "defer", lambda self, delay: delays.append(delay))
    inventory, _ = refresh(provider)
    assert inventory.discovery_errors == []
    assert posts == 2
    assert delays == [12]
    assert waits.await_count == 4  # Probe list, refused POST, retried POST, results.


@pytest.mark.parametrize("retry_after", ["999999", "inf", "NaN"])
def test_over_cap_429_stops_without_early_retry(retry_after):
    provider = Provider()
    provider.override = lambda request: httpx.Response(429, headers={"Retry-After": retry_after}) if request.method == "POST" else None
    inventory, _ = refresh(provider)
    assert "globalping_rate_limited" in inventory.discovery_errors
    assert len([request for request in provider.requests if request.method == "POST"]) == 1


def test_exhausted_429_retries_are_bounded():
    provider = Provider()
    provider.override = lambda request: httpx.Response(429, headers={"Retry-After": "0"}) if request.method == "POST" else None
    inventory, _ = refresh(provider)
    assert "globalping_rate_limited" in inventory.discovery_errors
    assert len([request for request in provider.requests if request.method == "POST"]) == discovery.REQUEST_ATTEMPTS


def test_retry_after_supports_http_dates_and_malformed_values():
    date = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)
    assert 18 < discovery._retry_after(date, 0) <= 20
    assert discovery._retry_after("garbage", 1) == 2
    assert discovery._retry_after(None, 2) == 4
    assert discovery._retry_after("-10", 0) == 0


def test_pacer_is_shared_and_rechecks_cooldown(monkeypatch):
    async def run():
        pacer = discovery._ApiPacer()
        loop = asyncio.get_running_loop()
        started = loop.time()
        pacer.defer(0.02)
        await asyncio.gather(pacer.wait(), pacer.wait(), pacer.wait())
        assert loop.time() - started >= 0.03

    monkeypatch.setattr(discovery, "API_INTERVAL", 0.01)
    asyncio.run(run())


@pytest.mark.parametrize("url,code", [(discovery.POPS_URL, "inventory_fetch_failed"),
                                     (discovery.RANGES_URL, "public_ranges_fetch_failed")])
def test_required_sources_fail_with_safe_discovery_error(url, code, monkeypatch):
    monkeypatch.setattr(discovery, "REQUEST_ATTEMPTS", 1)
    provider = Provider()

    def fail(request):
        if str(request.url) == url:
            raise httpx.ConnectError("authorization private-secret", request=request)

    provider.override = fail
    with pytest.raises(DiscoveryError, match=f"^{code}$") as error:
        refresh(provider, previous())
    assert error.value.__suppress_context__
    assert "private-secret" not in str(error.value)


def test_no_redirects_or_remote_measurement_location_are_followed():
    provider = Provider()
    provider.override = lambda request: httpx.Response(302, headers={"Location": "https://other.invalid/leak"}) if request.method == "POST" else None
    inventory, _ = refresh(provider, config=settings(globalping_token="private-token"), follow_redirects=True)
    assert "globalping_http_error" in inventory.discovery_errors
    assert all(request.url.host != "other.invalid" for request in provider.requests)


def test_untrusted_measurement_id_cannot_redirect_bearer():
    provider = Provider()
    provider.override = lambda request: httpx.Response(202, json={"id": "../../evil?secret", "probesCount": 3}) if request.method == "POST" else None
    inventory, _ = refresh(provider)
    assert "globalping_invalid_measurement" in inventory.discovery_errors
    assert not any("/measurements/" in request.url.path for request in provider.requests)


def test_ambiguous_post_network_error_is_not_retried():
    provider = Provider()

    def fail(request):
        if request.method == "POST":
            raise httpx.ReadTimeout("private-secret", request=request)

    provider.override = fail
    inventory, _ = refresh(provider)
    assert "globalping_unavailable" in inventory.discovery_errors
    assert len([request for request in provider.requests if request.method == "POST"]) == 1


def test_polling_is_bounded_and_preserves_finished_partial_answers(monkeypatch):
    monkeypatch.setattr(discovery, "MAX_POLLS", 2)
    provider = Provider(results=measurement(status="in-progress"))
    inventory, _ = refresh(provider)
    assert "globalping_poll_timeout" in inventory.discovery_errors
    assert len(inventory.mappings) == 1
    assert len([request for request in provider.requests if request.url.path == "/v1/measurements/test-id"]) == 2


def test_poll_wall_timeout_returns_partial_inventory(monkeypatch):
    monkeypatch.setattr(discovery, "POLL_TIMEOUT", 0.01)
    provider = Provider(results=measurement(status="in-progress"))
    monkeypatch.setattr(discovery, "POLL_INTERVAL", 1)
    inventory, _ = refresh(provider)
    assert "globalping_poll_timeout" in inventory.discovery_errors
    assert len(inventory.pops) == 1
    assert len(inventory.mappings) == 1


@pytest.mark.parametrize("body", [None, {}, {"results": []}, measurement(results=[]),
                                  measurement([dns_result(status="failed")]),
                                  measurement([dns_result(statusCode=3)]),
                                  measurement([dns_result(answers=None)]),
                                  measurement([{"probe": {"city": "Amsterdam"}, "result": None}])])
def test_invalid_or_missing_dns_results_are_errors(body):
    provider = Provider()
    provider.override = lambda request: httpx.Response(200, json=body) if "/measurements/" in request.url.path else None
    inventory, edge = refresh(provider)
    assert inventory.discovery_errors
    assert inventory.mappings == []
    edge.request.assert_not_awaited()


def test_authenticated_measurement_is_bulk_and_edge_requests_respect_concurrency():
    pops = [(f"{i:03X}", f"City {i}") for i in range(23)]
    provider = Provider(html(pops), cities=[city for _, city in pops])
    created = {}
    active = peak = 0

    def respond(request):
        if request.method == "POST":
            payload = json.loads(request.content)
            assert "limit" not in payload
            assert len(payload["locations"]) <= 166
            assert all(location["limit"] == 3 for location in payload["locations"])
            key = f"batch-{len(created)}"
            created[key] = payload["locations"]
            return httpx.Response(202, json={"id": key, "probesCount": len(created[key]) * 3})
        if "/measurements/" in request.url.path:
            locations = created[request.url.path.rsplit("/", 1)[1]]
            return httpx.Response(200, json=measurement([
                dns_result(location["city"], ips=(f"151.101.0.{int(location['city'].split()[-1]) + 1}",))
                for location in locations
            ]))

    async def verify(ip, ranges, operation):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return edge_result(ip, f"{int(ip.rsplit('.', 1)[1]) - 1:03X}")

    provider.override = respond
    edge = AsyncMock()
    edge.request.side_effect = verify
    inventory, _ = refresh(provider, edge=edge,
                           config=settings(globalping_token="private-token"))
    assert peak == 8
    assert len(created) == 1
    assert len(inventory.mappings) == len(inventory.pops) == 23
    assert inventory.discovery_errors == []


def test_cancellation_propagates():
    edge = AsyncMock()
    edge.request.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        refresh(Provider(), edge=edge)


@pytest.mark.parametrize("probes", [1, 3, 5])
def test_only_missing_pop_cities_get_dns_including_partly_covered_shared_city(probes):
    pops = (("AMS", "Amsterdam"), ("RTM", "Amsterdam"), ("PAR", "Paris"), ("LHR", "London"))
    ip3, ip4 = "151.101.3.1", "151.101.4.1"
    old = previous(pops=pops, ips=(("AMS", IP), ("PAR", IP2)))
    # Paris is absent from Globalping, but its POP is already freshly verified.
    provider = Provider(html(pops), cities=("Amsterdam", "London"), results=measurement([
        dns_result("Amsterdam", (ip3,)), dns_result("London", (ip4,)),
    ]))
    edge = AsyncMock()
    actual = {IP: "AMS", IP2: "PAR", ip3: "RTM", ip4: "LHR"}
    edge.request.side_effect = lambda ip, ranges, operation: edge_result(ip, actual[ip])

    def require_previous_verification_first(request):
        if request.url.host == "api.globalping.io":
            assert edge.request.await_count == 2

    provider.override = require_previous_verification_first
    config = settings() if probes == 3 else settings(warmer_probes_per_city=probes)
    assert config.warmer_probes_per_city == probes
    inventory, _ = refresh(provider, old, edge, config)
    payloads = [json.loads(request.content) for request in provider.requests if request.method == "POST"]
    assert len(payloads) == 1
    assert {location["city"] for location in payloads[0]["locations"]} == {"Amsterdam", "London"}
    assert all(location["limit"] == probes for location in payloads[0]["locations"])
    assert "limit" not in payloads[0]
    assert {mapping.pop for mapping in inventory.mappings} == {pop[0] for pop in pops}
    assert inventory.discovery_errors == []


def test_changed_actual_pop_leaves_former_city_eligible_for_dns():
    pops = (("AMS", "Amsterdam"), ("LHR", "London"))
    old = previous(pops=pops)
    provider = Provider(html(pops), cities=("Amsterdam", "London"),
                        results=measurement([dns_result(ips=(IP2,))]))
    edge = AsyncMock()
    edge.request.side_effect = lambda ip, ranges, operation: edge_result(ip, "LHR" if ip == IP else "AMS")
    inventory, _ = refresh(provider, old, edge)
    payload = json.loads(next(request.content for request in provider.requests if request.method == "POST"))
    assert payload["locations"] == [{"city": "Amsterdam", "limit": 3}]
    assert [(mapping.pop, mapping.ip) for mapping in inventory.mappings] == [("AMS", IP2), ("LHR", IP)]
    assert inventory.discovery_errors == []
    assert old.mappings[0].pop == "AMS" and old.mappings[0].verified_at == OLD


@pytest.mark.parametrize("status,error", [(401, "globalping_auth_failed"),
                                         (429, "globalping_rate_limited"),
                                         (503, "globalping_http_error")])
def test_all_fresh_previous_candidates_survive_later_dns_failure(status, error):
    pops = (("AMS", "Amsterdam"), ("DEL", "Delhi"))
    old = previous(pops=pops, ips=(("AMS", IP), ("AMS", IP2)))
    provider = Provider(html(pops), cities=("Amsterdam", "Delhi"))
    edge = AsyncMock()
    edge.request.side_effect = lambda ip, ranges, operation: edge_result(ip)

    def fail(request):
        if request.url.host == "api.globalping.io":
            # Both saved addresses, including the backup for the same POP, must
            # be reverified before any Globalping request can fail or exhaust quota.
            assert edge.request.await_count == 2
        if request.method == "POST":
            assert json.loads(request.content)["locations"] == [{"city": "Delhi", "limit": 3}]
            return httpx.Response(status, headers={"Retry-After": "999999"})

    provider.override = fail
    inventory, _ = refresh(provider, old, edge)
    assert {mapping.ip for mapping in inventory.mappings} == {IP, IP2}
    assert all(mapping.verified_at > OLD for mapping in inventory.mappings)
    assert {pop.code for pop in inventory.pops} == {"AMS", "DEL"}
    assert inventory.discovery_errors == [error, "missing_mappings:DEL"]
    assert old.discovery_errors == ["old_failure"]


def test_daily_refreshes_resume_from_persisted_partial_bulk_discovery():
    pops = (("AMS", "Amsterdam"), ("CHI", "Chicago"), ("DFW", "Dallas"))
    ips = {"Amsterdam": IP, "Chicago": IP2, "Dallas": "151.101.3.1"}
    actual = {ip: code for (code, city), ip in zip(pops, ips.values())}
    old = None
    submitted_by_day = []
    for day, expected_coverage in ((1, 1), (2, 3), (3, 3)):
        provider = Provider(html(pops), cities=tuple(ips))
        posted = []

        def respond(request):
            if request.method == "POST":
                payload = json.loads(request.content)
                assert all(location["limit"] == 3 for location in payload["locations"])
                posted.extend(location["city"] for location in payload["locations"])
            if "/measurements/" in request.url.path:
                cities = (posted[:1] if day == 1 else posted)
                return httpx.Response(200, json=measurement([
                    dns_result(city, (ips[city],)) for city in cities
                ]))

        provider.override = respond
        edge = AsyncMock()
        edge.request.side_effect = lambda ip, ranges, operation: edge_result(ip, actual[ip])
        inventory, _ = refresh(provider, old, edge,
                               settings(globalping_token="private-token", warmer_concurrency=1))
        assert len(inventory.pops) == 3
        assert len(inventory.mappings) == expected_coverage
        assert all(mapping.verified_at > OLD for mapping in inventory.mappings)
        submitted_by_day.append(posted)
        # Round-trip the actual persistence representation and use a new Discovery
        # each day: no in-memory cursor or model changes are needed for resumption.
        old = Inventory.model_validate_json(inventory.model_dump_json())
    assert submitted_by_day == [["Amsterdam", "Chicago", "Dallas"], ["Chicago", "Dallas"], []]


def test_authenticated_bulk_answers_can_cover_multiple_pop_cities():
    pops = (("AMS", "Amsterdam"), ("CHI", "Chicago"), ("DFW", "Dallas"))
    ip3 = "151.101.3.1"
    provider = Provider(html(pops), cities=("Amsterdam", "Chicago", "Dallas"))
    posted = []
    verified = []

    def respond(request):
        if request.method == "POST":
            posted.extend(location["city"] for location in json.loads(request.content)["locations"])
        if "/measurements/" in request.url.path:
            return httpx.Response(200, json=measurement([
                dns_result("Amsterdam", (IP, IP2)),
                dns_result("Chicago", (ip3,)),
                dns_result("Dallas", (IP2,)),
            ]))

    async def verify(ip, ranges, operation):
        verified.append(ip)
        return edge_result(ip, {IP: "AMS", IP2: "DFW", ip3: "CHI"}[ip])

    provider.override = respond
    edge = AsyncMock()
    edge.request.side_effect = verify
    inventory, _ = refresh(provider, edge=edge,
                           config=settings(globalping_token="private-token", warmer_concurrency=1))
    assert posted == ["Amsterdam", "Chicago", "Dallas"]
    assert len(inventory.mappings) == len(inventory.pops) == 3
    assert inventory.discovery_errors == []


def test_poll_quota_error_keeps_previous_and_partial_dns_candidates_even_when_all_mapped():
    pops = (("AMS", "Amsterdam"), ("DEL", "Delhi"))
    provider = Provider(html(pops), cities=("Amsterdam", "Delhi"))
    polls = 0

    def respond(request):
        nonlocal polls
        if "/measurements/" in request.url.path:
            polls += 1
            if polls == 1:
                return httpx.Response(200, json=measurement([dns_result("Delhi", (IP2,))], status="in-progress"))
            return httpx.Response(429, headers={"Retry-After": "999999"})

    provider.override = respond
    edge = AsyncMock()
    edge.request.side_effect = lambda ip, ranges, operation: edge_result(ip, "AMS" if ip == IP else "DEL")
    inventory, _ = refresh(provider, previous(pops=pops), edge)
    assert {mapping.pop for mapping in inventory.mappings} == {"AMS", "DEL"}
    assert inventory.discovery_errors == ["globalping_rate_limited"]
    assert edge.request.await_count == 2
    assert polls == 2


def test_disappeared_target_still_counts_when_all_previous_mappings_reverify():
    old = previous(pops=(("AMS", "Amsterdam"), ("RTM", "Amsterdam")), ips=(("AMS", IP), ("RTM", IP2)))
    edge = AsyncMock()
    edge.request.side_effect = lambda ip, ranges, operation: edge_result(ip, "AMS" if ip == IP else "RTM")
    provider = Provider()
    inventory, _ = refresh(provider, old, edge)
    assert {pop.code for pop in inventory.pops} == {"AMS", "RTM"}
    assert {mapping.pop for mapping in inventory.mappings} == {"AMS", "RTM"}
    assert inventory.discovery_errors == ["inventory_targets_absent:RTM"]
    assert all(request.url.host != "api.globalping.io" for request in provider.requests)


def test_failed_previous_address_is_not_carried_forward_as_fresh():
    provider = Provider(results=measurement([dns_result(ips=(IP2,))]))
    edge = AsyncMock()
    edge.request.side_effect = lambda ip, ranges, operation: edge_result(ip, success=ip != IP)
    inventory, _ = refresh(provider, previous(), edge)
    assert [mapping.ip for mapping in inventory.mappings] == [IP2]
    assert inventory.discovery_errors == ["edge_verification_failed"]
    assert any(request.method == "POST" for request in provider.requests)


def mock_local_dns(monkeypatch, resolver):
    monkeypatch.setattr(Discovery, "_local_candidates", LOCAL_CANDIDATES)
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolver)


def addrinfo(ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    address = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "ignored-cname.invalid", address)


def test_local_dns_uses_configured_hostname_and_can_avoid_globalping(monkeypatch):
    resolver = AsyncMock(return_value=[addrinfo(IP), addrinfo(IP)])
    mock_local_dns(monkeypatch, resolver)
    provider = Provider()
    inventory, edge = refresh(provider, config=settings(stellate_url="https://other.stellate.sh/graphql"))
    resolver.assert_awaited_once_with("other.stellate.sh", 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    edge.request.assert_awaited_once_with(IP, parse_ranges(RANGES), PROBE)
    assert inventory.discovery_errors == []
    assert [mapping.pop for mapping in inventory.mappings] == ["AMS"]
    assert all(request.url.host != "api.globalping.io" for request in provider.requests)


def test_local_dns_filters_unsafe_and_unpublished_answers_before_edge(monkeypatch):
    ipv6 = "2a04:4e42::1"
    resolver = AsyncMock(return_value=[addrinfo(ip) for ip in (
        IP, ipv6, "127.0.0.1", "169.254.169.254", "100.64.0.1", "8.8.8.8", "::1", "fe80::1",
    )])
    mock_local_dns(monkeypatch, resolver)
    edge = AsyncMock()
    edge.request.side_effect = lambda ip, ranges, operation: edge_result(ip)
    inventory, _ = refresh(Provider(), edge=edge)
    assert {call.args[0] for call in edge.request.await_args_list} == {IP, ipv6}
    assert {mapping.ip for mapping in inventory.mappings} == {IP, ipv6}
    assert inventory.discovery_errors == ["local_dns_unsafe_answer"]


@pytest.mark.parametrize("failure", [socket.gaierror("private resolver details"), OSError("private network details")])
def test_local_dns_socket_failure_is_reported_without_hiding_globalping_success(monkeypatch, failure):
    mock_local_dns(monkeypatch, AsyncMock(side_effect=failure))
    inventory, _ = refresh(Provider())
    assert [mapping.pop for mapping in inventory.mappings] == ["AMS"]
    assert inventory.discovery_errors == ["local_dns_unavailable"]
    assert "private" not in inventory.model_dump_json()


def test_local_dns_deadline_is_bounded_and_error_is_retained(monkeypatch):
    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(discovery, "LOCAL_DNS_TIMEOUT", 0.01)
    mock_local_dns(monkeypatch, AsyncMock(side_effect=stalled))
    inventory, _ = refresh(Provider())
    assert [mapping.pop for mapping in inventory.mappings] == ["AMS"]
    assert inventory.discovery_errors == ["local_dns_timeout"]


def test_empty_local_dns_answer_is_not_silently_successful(monkeypatch):
    mock_local_dns(monkeypatch, AsyncMock(return_value=[]))
    inventory, _ = refresh(Provider())
    assert inventory.discovery_errors == ["local_dns_no_answers"]


def test_local_and_saved_candidates_are_verified_before_globalping_quota_failure(monkeypatch):
    pops = (("AMS", "Amsterdam"), ("LHR", "London"), ("RTM", "Amsterdam"))
    resolver = AsyncMock(return_value=[addrinfo(IP), addrinfo(IP2)])
    mock_local_dns(monkeypatch, resolver)
    provider = Provider(html(pops), cities=("Amsterdam", "London"))
    edge = AsyncMock()
    edge.request.side_effect = lambda ip, ranges, operation: edge_result(ip, "AMS" if ip == IP else "LHR")

    def fail(request):
        if request.url.host == "api.globalping.io":
            assert edge.request.await_count == 2
            resolver.assert_awaited_once()
        if request.method == "POST":
            # London was filled locally, but Amsterdam still lacks its RTM POP.
            assert json.loads(request.content)["locations"] == [{"city": "Amsterdam", "limit": 3}]
            return httpx.Response(429, headers={"Retry-After": "999999"})

    provider.override = fail
    inventory, _ = refresh(provider, previous(pops=pops), edge)
    assert {mapping.pop for mapping in inventory.mappings} == {"AMS", "LHR"}
    assert {pop.code for pop in inventory.pops} == {"AMS", "LHR", "RTM"}
    assert inventory.discovery_errors == ["globalping_rate_limited", "missing_mappings:RTM"]
    assert [call.args[0] for call in edge.request.await_args_list] == [IP, IP2]


def test_full_saved_coverage_skips_even_local_dns(monkeypatch):
    resolver = AsyncMock(side_effect=AssertionError("already verified: no DNS needed"))
    mock_local_dns(monkeypatch, resolver)
    inventory, edge = refresh(Provider(), previous())
    resolver.assert_not_awaited()
    edge.request.assert_awaited_once()
    assert inventory.discovery_errors == []


def test_unknown_actual_pop_from_local_dns_never_expands_inventory(monkeypatch):
    mock_local_dns(monkeypatch, AsyncMock(return_value=[addrinfo(IP2)]))
    edge = AsyncMock()
    edge.request.side_effect = lambda ip, ranges, operation: edge_result(ip, "ZZZ" if ip == IP2 else "AMS")
    inventory, _ = refresh(Provider(), edge=edge)
    assert [pop.code for pop in inventory.pops] == ["AMS"]
    assert [mapping.ip for mapping in inventory.mappings] == [IP]
    assert inventory.discovery_errors == ["edge_unknown_pop"]
