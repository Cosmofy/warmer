# Cosmofy Warmer

Follow `/Users/sour/Downloads/COSMOFY_NEW_MICROSERVICE_GUIDE.md` and current APOD/News/Articles conventions. This service owns Fastly/Stellate POP discovery and named GraphQL query warming; it does not own Java's schema or purge caches automatically.

- FastAPI, uv, Python 3.14, separate routers, config, errors, observability and telemetry modules.
- `extract_ip` refreshes the target POP inventory and verifies candidate addresses. A missing mapping must never remove a target from the coverage denominator.
- `warm articles` and `warm picture` select server-owned queries. No arbitrary URL, query, IP, authentication header or GraphQL mutation is accepted from callers.
- Every target POP must be successfully reached for a run to be complete. Verify the actual POP header and GraphQL payload. HTTP 200 alone is insufficient. Report partial coverage explicitly.
- Respect TLS verification and the original hostname when directing a request to a candidate IP. Only globally routable, published Fastly IP ranges are permitted. No subnet scanning.
- Persist inventory, mappings and run outcomes; stale discovery and provider failures must not masquerade as complete coverage.
- Bounded concurrency, request deadlines, safe retries, single active operation and fail-closed administration authentication.
- No AWS application credentials. Private OTLP collector exports to the shared AWS/Loki systems.
- Do not modify sibling services or shared infrastructure as a side effect. Deployment and EventBridge activation must be explicitly reported, never implied by templates.
- Commits: one relevant emoji followed by entirely lowercase text. No green check-mark emoji.

## Query ownership

`articles` is the full current Java `articles` selection. `picture` is a friendly name for the modern Java `apod` field (not its legacy MongoDB-backed `picture` field), with today's date resolved in America/Denver and checked in the response. Warming uses the same default-date operation as the client unless an explicit-date operation is separately configured in code.
