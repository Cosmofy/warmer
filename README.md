# warmer

Cosmofy's FastAPI service for discovering Stellate/Fastly POP addresses and warming server-owned GraphQL queries. Built in the same format as APOD, News and Articles. Version 1.0.0.

Current verified status: [live test results and coverage gaps](docs/LIVE_VERIFICATION.md). The initial live run mapped 30 of 164 targets; full worldwide coverage and production activation are not complete.

## Operations

| Method | Path | Purpose |
|---|---|---|
| POST | `/extract-ip` | Start daily inventory/address discovery |
| POST | `/warm/articles` | Warm the full registered Articles query |
| POST | `/warm/picture` | Warm the modern Java APOD query for today in Mountain Time |
| GET | `/runs/{id}` | Inspect a run's progress, coverage and per-POP outcomes |
| GET | `/runs` | List the twenty latest runs |
| GET | `/pops` | Inspect inventory, verified mappings, freshness and gaps |
| GET | `/queries` | List registered operation names |
| GET | `/health/live` | Process liveness |
| GET | `/health/ready` | Local job-storage readiness |

All operations except health require `Authorization: Bearer <WARMER_API_TOKEN>`. This is an administration API: a public unauthenticated call could trigger hundreds of requests. Request bodies cannot supply queries, URLs, addresses, credentials or mutations.

POST requests return **202 Accepted** with a run ID and `Location` pointing to its status. Accepted does not mean completed. Only `status: complete` means the operation covered every target; discovery additionally requires no unresolved discovery errors. Warming evaluates fresh mappings and actual responses independently. `incomplete`, `failed`, and `interrupted` must be treated as unsuccessful by automation.

## Run locally

Requires Python 3.14, uv and curl with HTTPS support. The application independently caps subprocess output and bounds cleanup, regardless of curl's own size-limit support. No Redis, Turso or AWS account is required to run locally.

```bash
uv sync --locked --dev
```

Create a private `.env` containing `WARMER_API_TOKEN=<random-token-at-least-32-characters>`. Generate the token with `openssl rand -hex 32`. A private local `.env` was created during initial implementation; it is not committed.

```bash
PYTHONDONTWRITEBYTECODE=1 uv run uvicorn app.main:app --host 127.0.0.1 --port 29404 --workers 1
```

Open `http://127.0.0.1:29404/docs`, authorize with the token, then POST `/extract-ip`. Poll its run URL; inspect `/pops` before warming. No discovery, warming or purge happens just because the app starts. No recurring local scheduler is installed.

Run tests (no production network calls):

```bash
PYTHONDONTWRITEBYTECODE=1 uv run pytest -v
```

## How discovery works

1. Read Fastly's published complete POP table and public IP ranges. The count is discovered, not hardcoded to an old marketing number.
2. Preserve all previously known target codes if the provider list shrinks. A retired/renamed POP therefore stays unresolved until an operator reviews the inventory; disappearing targets cannot make coverage suddenly look complete.
3. Reverify previous mappings and try bounded local DNS first. Only unresolved POP cities consume regional Globalping probes. Each batch is verified before planning the next, so new coverage can avoid unnecessary probes. Three probes per requested city remains the default.
4. Test candidates against the actual Stellate hostname, retaining TLS verification. Validate `x-served-by` to identify the actual POP; store only globally routable addresses in Fastly's published ranges. A city's DNS result is only a candidate, not a guaranteed route to that city. Probes may be unavailable or rate limited.
5. Save fresh mappings and errors. Missing POPs remain in the denominator. Partial discovery is useful evidence but is never complete.

Globalping is an external discovery dependency. Its measurements are public: only the public Stellate hostname is submitted, never API tokens, GraphQL payloads or private infrastructure addresses. With `GLOBALPING_TOKEN`, the daily discovery packs up to 500 tests into each measurement (166 cities at the default three probes per city); without a token it stays within the anonymous 50-test limit. Candidate IP verification has a separate bounded concurrency. Warmer does not enumerate or scan IP ranges.

## How warming works

The application resolves a name to its fixed query in `app/queries.py`, then issues HTTPS POSTs to Stellate using the discovered addresses. It uses curl `--resolve` through an async subprocess: the destination address changes, the original Host/SNI and certificate verification do not. This is the same mechanism proven in the manual twenty-POP MISS-to-HIT test.

- Default concurrency: eight. One active discovery/warming job; repeat triggers while busy return 409.
- One request per mapped POP normally; up to three verified alternate addresses may be tried after failures. The report includes every attempt. No infinite retry loop.
- A valid HTTP 200 GraphQL result with the intended POP and `gcdn-cache: MISS` or `HIT` counts as a successful warming request. MISS retrieves data and is the normal cold-cache result; it is **not** a claim that a second HIT was independently observed. This default deliberately avoids doubling every warming pass.
- HTTP errors, GraphQL errors, wrong POP, absent/stale mapping, uncacheable/stale responses and wrong APOD date do not count. Jobs keep partial progress on timeout/shutdown.
- No automatic purge. If today's default-date APOD is still stale at an edge, Warmer reports failure; it must not mark yesterday's picture as today's success. Purge/expiry policy belongs to separately coordinated content-refresh automation.
- No guarantee that a warm entry survives until tomorrow: the actual Stellate TTL, SWR, eviction and query scope still apply. Running discovery every 24 hours does not change those policies.

### Query ownership

`picture` intentionally maps to Java's **`apod`**, not the older MongoDB-backed `picture` field. It selects the entire modern APOD object without a date argument, matching the default-today operation, and validates the response date in `America/Denver`.

`articles` selects id, title, subtitle, month, year, url, source, authors and banner. These selections follow the inspected Java schema. Coordinate app selections with Java: warming a superset is not a universal promise that every query/argument combination hits, even with partial-query caching. Different date arguments need their own operation if required. Do not add caller-supplied query text to work around this.

## State, safety and observability

SQLite defaults to `data/warmer.db`; set `WARMER_STATE_DB` to a persistent private directory in production. Inventory and run history survive restarts. A process file lock enforces one worker/instance per database; unfinished jobs are marked interrupted on restart. Do not deploy independent active replicas behind a load balancer with separate state and pretend jobs are coordinated.

`observability.py` emits structured logs with service/node, request/run ID, trace/span IDs, action, target/actual POP, duration and outcome. `telemetry.py` exports to a private collector only when an OTLP endpoint is explicitly configured. No AWS credentials belong in this application. See `deploy/` and [deployment instructions](docs/DEPLOYMENT.md) for prepared AWS/Loki collector templates; templates are not an active deployment.

## Scheduling and Java

The intended daily trigger is EventBridge -> POST `/extract-ip`. Warming triggers use POST `/warm/{name}` and must inspect the returned job, not just HTTP 202. A Java wrapper should expose an authenticated GraphQL **mutation**, not a cacheable query, and call Warmer directly. Warmer then calls Stellate for the registered read operation. See [Java handoff](docs/GRAPHQL_INTEGRATION.md).

GitHub Actions runs verbose offline tests on every push/PR. Deployment is a separate guarded manual workflow, pending approved single-instance bootstrap. Initial implementation does not alter existing AWS rules, DNS, APIs or collectors.

## Sources and discovery scope

- [Fastly POP inventory](https://www.fastly.com/documentation/guides/getting-started/concepts/using-fastlys-global-pop-network/)
- [Fastly public IP ranges](https://api.fastly.com/public-ip-list)
- [Stellate edge locations](https://stellate.co/docs/graphql-edge-cache/locations)
- [Globalping API](https://globalping.io/docs/api.globalping.io)

Complete worldwide live coverage is **not claimed by the presence of this repository**. The persisted `/pops` inventory and completed run reports are the evidence. Every target must be covered; missing candidates are a deployment/coverage gap to resolve, not something to omit from the target list.
