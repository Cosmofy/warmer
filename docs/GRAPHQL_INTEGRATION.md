# Livia Java integration: Warmer

Warmer is a privileged administrative REST service. It is not a GraphQL subgraph and does not introduce federation. No Java changes were made by this implementation.

## Current status

Local service: `http://127.0.0.1:29404`. A production endpoint and EventBridge schedule have not been activated; use `WARMER_SERVICE_URL` only after the actual deployment is verified. Do not advertise a proposed DNS name as live.

## Java contract

Keep the public operation an authenticated/authorized **mutation** because it triggers work. Suggested names:

```graphql
enum WarmResource { ARTICLES PICTURE }
type Mutation {
  warm(resource: WarmResource!): WarmerJob!
  refreshWarmerIps: WarmerJob!
}
```

Map ARTICLES -> POST `/warm/articles`, PICTURE -> POST `/warm/picture`, refresh -> POST `/extract-ip`. Do not accept a raw query, arbitrary endpoint or IP from clients. Do not route the administrative mutation back through a cacheable read query. Administrative operations must not be edge-cached.

Send `Authorization: Bearer <WARMER_API_TOKEN>` from private Java configuration, validated `x-request-id`, and current W3C trace context. Never send a client's arbitrary token as the Warmer service token. Apply real authorization in Java; the service token proves trusted service access, not end-user privileges.

## Accepted job and polling

202 with `Location: /runs/<id>` and JSON:

```json
{
  "id": "opaque-run-id",
  "operation": "warm",
  "query_name": "articles",
  "status": "running",
  "created_at": "2026-09-05T12:00:00Z",
  "finished_at": null,
  "target_pops": ["BOM", "LHR"],
  "covered_pops": [],
  "missing_pops": ["BOM", "LHR"],
  "results": [],
  "error": null
}
```

GET `/runs/{id}` with the same service authentication. The terminal statuses are `complete`, `incomplete`, `failed`, `interrupted`. Only complete meets the all-target requirement. Individual result fields are `target_pop`, `ip`, `actual_pop`, `server`, `status_code`, `cache_status`, `duration_ms`, `success`, `error`.

The target count is the length of `target_pops`; covered/missing arrays use actual target codes, not requested probe cities. Errors include missing_fresh_mapping, pop_mismatch, invalid_graphql_response, uncacheable_or_stale_response and transport failures. Do not replace incomplete with a boolean success just because the HTTP request itself succeeded.

GET `/pops` returns inventory/freshness/coverage. GET `/runs` returns the twenty latest jobs. GET `/queries` returns registered names. All require service authentication.

## Error handling

```json
{"error":{"code":"RUN_IN_PROGRESS","message":"Another discovery or warming run is already in progress."}}
```

- 401 UNAUTHORIZED: missing/wrong service token.
- 404 UNKNOWN_QUERY / RUN_NOT_FOUND: invalid operation or job ID.
- 409 RUN_IN_PROGRESS: do not blindly flood with retries; inspect active runs.
- 409 DISCOVERY_REQUIRED: no inventory or outdated inventory; refresh first.
- 422 INVALID_REQUEST: invalid path/input shape.
- 500 INTERNAL_ERROR or server failure: not successful acceptance; inspect logs before retrying because the previous attempt may have created a job.
- 503 SHUTTING_DOWN: the service is stopping; retry after restart.

Neither Java nor EventBridge should treat HTTP202 as proof the background job finished. Prevent overlapping triggers and persist/poll the returned job identifier. Initial API does not provide exactly-once delivery or durable queued jobs; the single active run rejects overlap, and interrupted jobs need an explicit fresh trigger.

## Registered query behavior

PICTURE uses the modern Java `apod` selection, defaults to today, and validates the Mountain Time date. It deliberately does not call the legacy Java `picture` field. ARTICLES uses the complete inspected Article/Author/Banner selection. Coordinate changes to those schemas and app selections with `app/queries.py`; do not assume all argument combinations are warmed.

No Java-side Redis response cache is needed for these administrative jobs. Poll the service's persistent run store. Retain structured request/run/trace IDs and report per-POP failures to the operator.

## Integration tests

Verify admin authorization, registered-name mapping, private service token use, 202 then polling, all terminal states, incomplete coverage surfaced, timeout/409 handling, and W3C propagation. A cacheable admin query, successful 202 mistaken for completed warm, or Java calling itself through Stellate is an integration bug.
