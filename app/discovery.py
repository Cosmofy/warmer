"""Refresh authoritative targets, then discover and freshly verify DNS candidates.

Only the official POP table and public ranges are fatal dependencies. All other
failures return an inventory with discovery_errors so the manager can persist
the full denominator without treating partial discovery as a successful refresh.
"""

import asyncio
import ipaddress
import math
import re
import unicodedata
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.models import Inventory, Mapping, Pop, utc_now
from app.provider import EdgeClient, allowed_ip
from app.queries import PROBE

POPS_URL = "https://www.fastly.com/documentation/guides/getting-started/concepts/using-fastlys-global-pop-network/"
RANGES_URL = "https://api.fastly.com/public-ip-list"
GLOBALPING_URL = "https://api.globalping.io/v1"
REQUEST_ATTEMPTS = 3
API_INTERVAL = 0.5
POLL_INTERVAL = 0.5
POLL_TIMEOUT = 60.0
MAX_POLLS = 100
RETRY_AFTER_CAP = 30.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024

# Conservative special-use exclusions also catch supernets with public endpoints
# but private interiors. No addresses are enumerated, generated, or scanned.
_SPECIAL_RANGES = tuple(ipaddress.ip_network(value) for value in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/3", "2001::/23", "2001:db8::/32", "2002::/16", "3fff::/20",
))


class DiscoveryError(RuntimeError):
    """Safe, service-owned error code; never an upstream body or exception text."""


class _FetchError(RuntimeError):
    pass


def _text(value: str) -> str:
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"<[^>]*>", "", value)
    return " ".join(unescape(value).replace("*", "").replace("`", "").split())


class _PopTable(HTMLParser):
    """Read only the first table following the actual complete-list heading."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading: list[str] | None = None
        self.found = False
        self.in_table = False
        self.done = False
        self.invalid = False
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if re.fullmatch(r"h[1-6]", tag):
            if self.found:
                self.done = True
            self.heading = []
        elif tag == "table" and self.found:
            if self.in_table:
                self.invalid = True
            self.in_table = True
        elif self.in_table and tag == "tr":
            if self.row is not None:
                self.invalid = True
            self.row = []
        elif self.in_table and tag in {"th", "td"}:
            if self.cell is not None or self.row is None:
                self.invalid = True
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.done:
            return
        if self.heading is not None:
            self.heading.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if self.done:
            return
        if re.fullmatch(r"h[1-6]", tag) and self.heading is not None:
            self.found = _text("".join(self.heading)).casefold() == "complete list of pops"
            self.heading = None
        elif self.in_table and tag in {"th", "td"} and self.cell is not None:
            if self.row is not None:
                self.row.append(_text("".join(self.cell)))
            self.cell = None
        elif self.in_table and tag == "tr" and self.row is not None:
            if self.cell is not None:
                self.invalid = True
            self.rows.append(self.row)
            self.row = None
        elif self.in_table and tag == "table":
            if self.row is not None or self.cell is not None:
                self.invalid = True
            self.in_table = False
            self.done = True


def _markdown_rows(document: str) -> list[list[str]]:
    found = False
    rows = []
    for line in document.splitlines():
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if heading:
            if found:
                break
            found = _text(heading[1]).casefold() == "complete list of pops"
            continue
        if not found:
            continue
        if "|" in line:
            cells = [_text(cell) for cell in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
                continue
            rows.append(cells)
        elif rows:
            break
    return rows


def parse_pops(document: str) -> list[Pop]:
    parser = _PopTable()
    parser.feed(document)
    parser.close()
    # An incomplete HTML table is an invalid source, not an invitation to parse
    # unrelated tables or embedded markdown further down the page.
    rows = parser.rows if parser.found else _markdown_rows(document)
    if parser.invalid or parser.in_table or len(rows) < 2:
        raise DiscoveryError("inventory_invalid")
    headers = [cell.casefold() for cell in rows[0]]
    try:
        city_index = headers.index("location")
        code_index = headers.index("pop identifier")
    except ValueError:
        raise DiscoveryError("inventory_invalid") from None
    pops = {}
    for row in rows[1:]:
        if len(row) != len(headers):
            raise DiscoveryError("inventory_invalid")
        city, code = row[city_index], row[code_index]
        if not city or not re.fullmatch(r"[A-Z0-9]{3}", code):
            raise DiscoveryError("inventory_invalid")
        if code in pops and pops[code].city != city:
            raise DiscoveryError("inventory_invalid")
        pops[code] = Pop(code=code, city=city)
    return sorted(pops.values(), key=lambda pop: pop.code)


def parse_ranges(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        raise DiscoveryError("public_ranges_invalid")
    ranges = set()
    for key, version in (("addresses", 4), ("ipv6_addresses", 6)):
        entries = payload.get(key)
        if not isinstance(entries, list):
            raise DiscoveryError("public_ranges_invalid")
        for entry in entries:
            try:
                if not isinstance(entry, str) or "/" not in entry:
                    raise ValueError
                network = ipaddress.ip_network(entry, strict=True)
                if (network.version != version or not network.is_global
                        or (version == 6 and not network.subnet_of(ipaddress.ip_network("2000::/3")))
                        or any(network.version == special.version and network.overlaps(special)
                               for special in _SPECIAL_RANGES)):
                    raise ValueError
            except ValueError:
                raise DiscoveryError("public_ranges_invalid") from None
            ranges.add(str(network))
    if not ranges:
        raise DiscoveryError("public_ranges_invalid")
    return sorted(ranges)


def _city_key(city: str) -> str:
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", city)
                            if not unicodedata.combining(c)).casefold().split())


def _candidate(value: object, ranges: list[str]) -> str | None:
    if not isinstance(value, str) or "%" in value:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if address.is_multicast or address.is_reserved or not allowed_ip(str(address), ranges):
        return None
    return str(address)


def _retry_after(value: str | None, attempt: int) -> float:
    fallback = float(2 ** attempt)
    if value is None:
        return fallback
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            seconds = (date.astimezone(UTC) - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return fallback
    if not math.isfinite(seconds):
        return RETRY_AFTER_CAP + 1
    return max(0.0, seconds)


class _ApiPacer:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.next_at = 0.0

    async def wait(self):
        async with self.lock:
            loop = asyncio.get_running_loop()
            # Recheck after sleeping: another in-flight request may add a 429
            # cooldown while this coroutine waits.
            while self.next_at > loop.time():
                await asyncio.sleep(self.next_at - loop.time())
            self.next_at = loop.time() + API_INTERVAL

    def defer(self, seconds: float):
        self.next_at = max(self.next_at, asyncio.get_running_loop().time() + seconds)


class Discovery:
    def __init__(self, client: httpx.AsyncClient, edge: EdgeClient, settings: Settings):
        self.client = client
        self.edge = edge
        self.settings = settings
        self._slots = asyncio.Semaphore(settings.warmer_concurrency)
        self._pacer = _ApiPacer()
        self._api_error: str | None = None

    async def _request(self, method: str, url: str, *, payload=None) -> httpx.Response:
        api = url.startswith(GLOBALPING_URL + "/")
        prefix = "globalping" if api else "source"
        for attempt in range(REQUEST_ATTEMPTS):
            async with self._slots:
                if api:
                    if self._api_error:
                        raise _FetchError(self._api_error)
                    await self._pacer.wait()
                    if self._api_error:
                        raise _FetchError(self._api_error)
                request = self.client.build_request(
                    method, url, json=payload,
                    timeout=self.settings.warmer_request_timeout,
                    # Fastly's negotiated markdown has been observed to contain
                    # only the metro-sites subset (14 rows), while HTML has the
                    # complete inventory. Still parse markdown if served anyway.
                    headers={"Accept": "application/json" if api or url == RANGES_URL else "text/html"},
                )
                # A shared client's default auth/cookies must not leak to these
                # public sources. Never follow redirects or upstream Location.
                for header in ("authorization", "proxy-authorization", "cookie"):
                    request.headers.pop(header, None)
                if api and self.settings.globalping_token:
                    request.headers["Authorization"] = "Bearer " + self.settings.globalping_token.get_secret_value()
                try:
                    async with asyncio.timeout(self.settings.warmer_request_timeout):
                        response = await self.client.send(request, auth=None, follow_redirects=False)
                except (httpx.HTTPError, TimeoutError):
                    # POST may already have created a billable measurement.
                    if method != "GET" or attempt == REQUEST_ATTEMPTS - 1:
                        raise _FetchError(f"{prefix}_unavailable") from None
                    response = None
            if response is None:
                await asyncio.sleep(2 ** attempt)
                continue
            if response.status_code == 429:
                delay = _retry_after(response.headers.get("Retry-After"), attempt)
                if delay > RETRY_AFTER_CAP or attempt == REQUEST_ATTEMPTS - 1:
                    if api:
                        self._api_error = "globalping_rate_limited"
                    raise _FetchError(f"{prefix}_rate_limited")
                if api:
                    self._pacer.defer(delay)
                else:
                    await asyncio.sleep(delay)
                continue
            if api and response.status_code in {401, 403}:
                self._api_error = "globalping_auth_failed"
                raise _FetchError(self._api_error)
            if response.status_code >= 500 and method == "GET" and attempt < REQUEST_ATTEMPTS - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            if api and method == "POST" and response.status_code == 422:
                raise _FetchError("globalping_locations_unavailable")
            if response.status_code != (202 if method == "POST" else 200):
                raise _FetchError(f"{prefix}_http_error")
            if len(response.content) > MAX_RESPONSE_BYTES:
                raise _FetchError(f"{prefix}_invalid_response")
            return response
        raise _FetchError(f"{prefix}_unavailable")

    async def _json(self, method: str, path: str, *, payload=None):
        response = await self._request(method, GLOBALPING_URL + path, payload=payload)
        try:
            return response.json()
        except ValueError:
            raise _FetchError("globalping_invalid_response") from None

    async def _available_cities(self) -> dict[str, str]:
        probes = await self._json("GET", "/probes")
        if not isinstance(probes, list):
            raise _FetchError("globalping_invalid_probes")
        cities = {}
        for probe in probes:
            location = probe.get("location") if isinstance(probe, dict) else None
            city = location.get("city") if isinstance(location, dict) else None
            if not isinstance(city, str) or not city.strip():
                raise _FetchError("globalping_invalid_probes")
            cities.setdefault(_city_key(city), city)
        return cities

    def _answers(self, body, cities, ranges, candidates, errors) -> set[str]:
        if not isinstance(body, dict) or not isinstance(body.get("results"), list):
            raise _FetchError("globalping_invalid_results")
        if body.get("type") != "dns" or body.get("target") != urlsplit(self.settings.stellate_url).hostname:
            raise _FetchError("globalping_invalid_results")
        completed = set()
        for item in body["results"]:
            result = item.get("result") if isinstance(item, dict) else None
            probe = item.get("probe") if isinstance(item, dict) else None
            city = probe.get("city") if isinstance(probe, dict) else None
            if not isinstance(result, dict) or not isinstance(city, str) or _city_key(city) not in cities:
                errors.add("globalping_invalid_results")
                continue
            if result.get("status") == "in-progress" and body.get("status") == "in-progress":
                continue
            if result.get("status") != "finished" or result.get("statusCode") != 0:
                errors.add("globalping_dns_failed")
                continue
            answers = result.get("answers")
            if not isinstance(answers, list):
                errors.add("globalping_invalid_results")
                continue
            accepted = False
            for answer in answers:
                if not isinstance(answer, dict):
                    errors.add("globalping_invalid_results")
                    continue
                if answer.get("type") not in {"A", "AAAA"}:
                    continue
                ip = _candidate(answer.get("value"), ranges)
                if ip is None or (":" in ip) != (answer["type"] == "AAAA"):
                    errors.add("globalping_unsafe_answer")
                    continue
                candidates.add(ip)
                accepted = True
            if accepted:
                completed.add(_city_key(city))
            else:
                errors.add("globalping_no_safe_answers")
        return completed

    async def _measure(self, cities: dict[str, str], ranges: list[str], errors: set[str]) -> set[str]:
        candidates: set[str] = set()
        try:
            async with asyncio.timeout(POLL_TIMEOUT):
                try:
                    created = await self._json("POST", "/measurements", payload={
                        "type": "dns",
                        "target": urlsplit(self.settings.stellate_url).hostname,
                        "locations": [{"city": city, "limit": self.settings.warmer_probes_per_city}
                                      for city in cities.values()],
                    })
                except _FetchError as error:
                    # A city's probes can vanish after GET /probes. Split only
                    # explicit rejected locations, never an ambiguous POST error.
                    if str(error) != "globalping_locations_unavailable" or len(cities) == 1:
                        raise
                    items = list(cities.items())
                    middle = len(items) // 2
                    for half in (items[:middle], items[middle:]):
                        candidates.update(await self._measure(dict(half), ranges, errors))
                    return candidates
                measurement_id = created.get("id") if isinstance(created, dict) else None
                if not isinstance(measurement_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", measurement_id):
                    raise _FetchError("globalping_invalid_measurement")
                if created.get("probesCount") == 0:
                    raise _FetchError("globalping_locations_unavailable")
                for _ in range(MAX_POLLS):
                    body = await self._json("GET", "/measurements/" + measurement_id)
                    completed = self._answers(body, cities, ranges, candidates, errors)
                    if body.get("status") != "in-progress":
                        if body.get("status") != "finished":
                            errors.add("globalping_measurement_failed")
                        if completed != cities.keys():
                            errors.add("globalping_missing_city_results")
                        return candidates
                    await asyncio.sleep(POLL_INTERVAL)
                errors.add("globalping_poll_timeout")
        except TimeoutError:
            errors.add("globalping_poll_timeout")
        except _FetchError as error:
            errors.add(str(error))
        return candidates

    async def _verify(self, candidates, inventory, checked, errors):
        known = {pop.code for pop in inventory.pops}

        async def verify(ip):
            async with self._slots:
                try:
                    async with asyncio.timeout(self.settings.warmer_request_timeout + 2):
                        result = await self.edge.request(ip, inventory.fastly_ranges, PROBE)
                except (httpx.HTTPError, TimeoutError, OSError):
                    errors.add("edge_verification_failed")
                    return
            if not result.success or result.status_code != 200 or not result.server:
                errors.add("edge_verification_failed")
            elif result.actual_pop not in known:
                errors.add("edge_unknown_pop")
            else:
                inventory.mappings.append(Mapping(
                    pop=result.actual_pop, ip=ip, verified_at=utc_now(), server=result.server,
                ))

        safe = set()
        for value in candidates:
            ip = _candidate(value, inventory.fastly_ranges)
            if ip is None:
                errors.add("unsafe_candidate")
            elif ip not in checked:
                safe.add(ip)
        addresses = sorted(safe)
        for offset in range(0, len(addresses), self.settings.warmer_concurrency):
            batch = addresses[offset:offset + self.settings.warmer_concurrency]
            checked.update(batch)
            await asyncio.gather(*(verify(ip) for ip in batch))

    async def refresh(self, previous: Inventory | None) -> Inventory:
        # Finish before the manager's outer job deadline so partial state can be
        # persisted. Caller cancellation still propagates normally.
        deadline = asyncio.get_running_loop().time() + self.settings.warmer_job_timeout * 0.9
        self._api_error = None
        try:
            async with asyncio.timeout_at(deadline):
                response = await self._request("GET", POPS_URL)
                pops = parse_pops(response.text)
        except (_FetchError, TimeoutError):
            raise DiscoveryError("inventory_fetch_failed") from None
        try:
            async with asyncio.timeout_at(deadline):
                response = await self._request("GET", RANGES_URL)
                ranges = parse_ranges(response.json())
        except (_FetchError, TimeoutError, ValueError):
            raise DiscoveryError("public_ranges_fetch_failed") from None

        errors: set[str] = set()
        targets = {pop.code: pop for pop in previous.pops} if previous else {}
        removed = targets.keys() - {pop.code for pop in pops}
        if removed:
            errors.add("inventory_targets_absent:" + ",".join(sorted(removed)))
        targets.update({pop.code: pop for pop in pops})
        inventory = Inventory(fetched_at=utc_now(), source=POPS_URL,
                              pops=sorted(targets.values(), key=lambda pop: pop.code), fastly_ranges=ranges)
        checked: set[str] = set()
        try:
            async with asyncio.timeout_at(deadline):
                await self._verify([mapping.ip for mapping in previous.mappings] if previous else [],
                                   inventory, checked, errors)
                try:
                    available = await self._available_cities()
                except _FetchError as error:
                    errors.add(str(error))
                    available = None  # Availability is advisory; isolated batches can still work.
                cities = {}
                for pop in inventory.pops:
                    key = _city_key(pop.city)
                    if available is not None and key not in available:
                        errors.add("globalping_city_unavailable:" + pop.code)
                    else:
                        cities.setdefault(key, available[key] if available is not None else pop.city)
                items = list(cities.items())
                # At most eight cities (40 probes with the largest setting) per
                # POST also fits Globalping's anonymous 50-probe measurement cap.
                batch_size = min(8, self.settings.warmer_concurrency)
                batches = [dict(items[i:i + batch_size]) for i in range(0, len(items), batch_size)]
                for offset in range(0, len(batches), self.settings.warmer_concurrency):
                    results = await asyncio.gather(*(self._measure(batch, ranges, errors)
                                                    for batch in batches[offset:offset + self.settings.warmer_concurrency]))
                    await self._verify(set().union(*results), inventory, checked, errors)
        except TimeoutError:
            errors.add("discovery_timeout")
        missing = targets.keys() - {mapping.pop for mapping in inventory.mappings}
        if missing:
            errors.add("missing_mappings:" + ",".join(sorted(missing)))
        inventory.mappings.sort(key=lambda mapping: (mapping.pop, mapping.ip))
        inventory.discovery_errors = sorted(errors)
        return inventory
