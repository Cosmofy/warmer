# Warmer deployment preparation

Status: templates only. No deployment has been authorized or activated. No remote
writes, SSH sessions, DNS changes, credentials, schedules, workflow dispatches,
service starts, commits or pushes were performed for this preparation. Application
implementation and business-function tests belong to the main build owner.

The target is one generic OCI instance. London is a candidate, not a selected host.
Do not reuse Toronto's two-node NLB deployment loop or assume the London host has
capacity, suitable network access, certificates, or available ports.

## Unverified allocations

| Item | Template suggestion | Status |
| --- | --- | --- |
| Application listener | `127.0.0.1:29404` | **UNVERIFIED PLACEHOLDER** |
| Private OTLP/HTTP receiver | `127.0.0.1:25318` | **UNVERIFIED PLACEHOLDER** |
| Collector health | `127.0.0.1:13136` | **UNVERIFIED PLACEHOLDER** |
| Caddy site/certificates | `warmer.example.invalid` | Deliberately unusable placeholder |
| Possible service domain | `warmer.api.cosmofy.services.deployim.com` | Naming convention only; DNS/TLS not approved |
| Host / OCI region | One instance, possibly London | Not selected or verified |
| AWS telemetry region | `eu-west-2` | Sibling convention, not OCI placement |

After deployment authority is granted, inspect the chosen host's listeners and
installed service/proxy/collector configurations. Checking this Mac or sibling
source files cannot establish port availability on that host. Record the host,
verification time and allocated ports here, then update the unit, Caddy upstream,
collector listeners, OTEL endpoint and workflow variable together. Keep app and
collector listeners private; do not add OCI ingress for those ports. No extra
gRPC or collector metrics listener is configured.

## Runtime contract and single-instance constraint

| Method / route | Deployment expectation |
| --- | --- |
| `POST /extract-ip` | Bearer-authenticated; durably accept discovery and return `202` with a run identifier |
| `POST /warm/{name}` | Bearer-authenticated; named `articles` or `picture` query; durable `202` acceptance |
| `GET /runs/{id}` | Bearer-authenticated; persistent status/outcome, including explicit partial coverage |
| `GET /pops` | Bearer-authenticated; persisted POP inventory/mappings and freshness |
| `GET /health/live` | Bounded process liveness, not a provider or collector probe |
| `GET /health/ready` | Local SQLite store and curl startup availability only; `200` ready or `503` unavailable |

Use `Authorization: Bearer <WARMER_API_TOKEN>`, never a query-string token. The token
must contain at least 32 ASCII characters with no whitespace. Configure
the server-owned upstream through `STELLATE_URL` (an HTTPS `.stellate.sh` hostname,
port 443, without credentials or query parameters). Callers cannot supply arbitrary
URLs, IPs, queries, headers or mutations. Caddy does not add authentication: the
application must fail closed for missing, blank or wrong admin tokens. Prove that
behavior before exposing any admin route. Do not log bearer headers or include
them in workflow inputs/artifacts. `202` means accepted, not complete coverage.

Persistent application state is `/var/lib/cosmofy-warmer/state.db`. Keep the database,
SQLite WAL/SHM sidecars and run history outside code releases. Use a local disk,
not independent copies on multiple nodes or an unreviewed network filesystem.
Exactly one active Uvicorn worker and one service instance are allowed initially.
An in-process lock is not a distributed lock; a second replica requires coordinated
shared-state ownership, durable deduplication and leader/lease behavior first.

Persistent records do not by themselves guarantee interrupted work resumes.
Verify the core's restart recovery semantics: accepted work survives, interrupted
runs are resumed safely or marked interrupted/failed, and never silently complete.
No systemd timer, cron job, GitHub schedule or in-app duplicate scheduler is added
by this deployment layer. Health checks and deployments never invoke either POST.

## Files and runtime layout

The unit, Caddy fragment and dedicated collector follow APOD/News/Articles formats.
Intentional differences are optional telemetry, no Redis dependency, one worker,
external SQLite state, and a `current` release symlink for code-only rollback.

```text
/home/ubuntu/services/warmer/
  .env                       # private application settings, mode 0600
  releases/<sha>-<run>-<try>/ # immutable-after-promotion code and locked .venv
  current -> releases/...    # only this code pointer changes on promotion
/var/lib/cosmofy-warmer/
  state.db                   # persistent application state; never rsynced
/etc/cosmofy/
  warmer-bootstrapped         # root-owned/readable marker: single-replica-v1
  warmer-otel.env             # optional, root-owned 0600 collector-only env
/etc/otelcol-contrib/warmer.yaml
/var/lib/otelcol-contrib/warmer/ # separate private journal cursor/export queues
```

`cosmofy-warmer.service` uses the established `ubuntu:ubuntu` account. Its
`StateDirectory=cosmofy-warmer`, directory mode `0700`, `UMask=0077` and
`ReadWritePaths=/var/lib/cosmofy-warmer` permit SQLite and sidecars while code and
home remain read-only inside the unit. Do not relocate state into `current/`.
Use a separate `otelcol-contrib` user plus `systemd-journal` membership for the
collector; never grant that account access to application state or the admin token.

The server `.env` is not inside a release. Start from `deploy/warmer.env.example`
only after approval; token and upstream values are intentionally blank. Set
`WARMER_STATE_DB=/var/lib/cosmofy-warmer/state.db` in the server environment before
bootstrap, as shown in that template. Systemd's `StateDirectory` creates a
directory; it does not configure the app's database path. Readiness checks local
storage and curl availability, never live Fastly/Stellate coverage or collector
health.

## Bootstrap checklist — future instructions, not activation

Do not run deployment scripts to bootstrap. Obtain separate explicit authority for
the host, network exposure, DNS/TLS, account access and any telemetry/scheduling
resources. Leave GitHub deployment gates absent or `false` throughout preparation.

1. Select one OCI host and verify the three ports, disk capacity, architecture,
   Python 3.14 support, existing `ubuntu` account, `/home/ubuntu/.local/bin/uv`,
   curl, Caddy imports and any collector-contrib binary/version. Inspect existing
   listeners and units read-only; do not replace shared files. Require strict SSH
   host verification from an independently trusted fingerprint, not runtime
   `ssh-keyscan` or `StrictHostKeyChecking=no`.
2. Review the core's SQLite-path setting, fail-closed bearer auth, async durable
   acceptance, single-operation guard, bounded shutdown and recovery behavior.
   Configure a private `0600` application `.env` on the host. Infrastructure owners
   supply any collector credential chain separately; never copy sibling secrets.
3. Provision `/home/ubuntu/services/warmer/releases` and a first reviewed release
   owned by `ubuntu`. Install its dependencies with
   `PYTHONDONTWRITEBYTECODE=1 /home/ubuntu/.local/bin/uv sync --locked --no-dev --python 3.14`.
   Set `current` to that release. Provision the state directory owned by
   `ubuntu:ubuntu` with mode `0700`; the app creates/opens `state.db` there. Use a
   consistent SQLite backup mechanism and a tested restore procedure.
4. Review/install only Warmer's unit and, if separately selected, collector unit,
   config, environment and private storage directory. Create the root-owned,
   non-ubuntu-writable bootstrap marker containing exactly `single-replica-v1`
   only once prerequisites are met. The marker is a start/preflight safeguard,
   not proof of deployment success. No CI job creates it or installs infrastructure.
5. Validate the unit with `systemd-analyze verify` and the collector configuration
   using the selected binary's `validate` subcommand in its collector-only
   environment. If exposing HTTPS, replace the `.invalid` Caddy name/certificate
   paths, review ingress restrictions and authenticate admin routes before import.
   Validate the entire existing Caddy configuration; preserve unrelated sites.
   Only an authorized operator may reload/enable/start those services.
6. Verify loopback liveness/readiness, the resolved SQLite path and permissions,
   bearer rejection, durable run recovery and one active worker. If HTTPS was
   approved, verify TLS and health through Caddy separately. Check an identifiable
   trace/log at each selected backend without exposing secrets. Only then mark
   bootstrap verified in the GitHub environment. Keep producers paused until the
   operation-specific acceptance checks and separate activation approval succeed.

The unit allows 60 seconds of graceful Uvicorn shutdown inside a 90-second systemd
stop window. Drain long background runs before release; do not assume those values
cover an entire discovery/warm operation. A restart can interrupt work even though
the database survives. After explicit test authority, prove restart recovery using
controlled dependencies before relying on it for production operations.

## CI and guarded manual updates

`tests.yml` runs on pushes and pull requests, and is reusable by the manual deploy
workflow. It installs Python 3.14 with uv, runs `uv sync --locked --dev`, suppresses
bytecode with `PYTHONDONTWRITEBYTECODE=1`, disables OTEL exporters, and runs
`uv run --locked pytest -v --junitxml=test-results/pytest.xml`. The JUnit artifact
is uploaded with `always()` and retained for 14 days. The test step creates
`test-results/` and discovers tests from the repository root without a hardcoded
test-directory argument; the upload path matches the JUnit output. Missing results after an
installation failure are a warning, not a passing test claim. Tests must mock
Fastly/Stellate and use temporary SQLite state. Dummy CI values are not credentials.
There is no automatic deploy on a push or a test success.

`deploy.yml` is `workflow_dispatch` only, from `main`, with literal confirmation
`deploy-warmer`, `runs_drained=true`, passing tests of the same revision, and the
protected `production` environment. Configure required reviewers and a main-only
deployment-branch policy before allowing dispatch; YAML cannot enforce that the
environment's reviewers have been configured. It deploys to exactly one host.

| Existing interface / explicit gate | Required value after approval |
| --- | --- |
| `vars.DEPLOY_HOST` | One verified hostname or IPv4 address; no default, list or SSH options |
| `secrets.DEPLOY_SSH_KEY` | Established deployment key, reused by name, never provisioned here |
| `secrets.DEPLOY_KNOWN_HOSTS` | Independently verified SSH host entries |
| `vars.WARMER_APP_PORT` | Verified application port matching the reviewed unit; no fallback to `29404` |
| `vars.WARMER_BOOTSTRAPPED` | Literal `true` only after host bootstrap verification |
| `vars.WARMER_DEPLOY_ENABLED` | Literal `true` only after deployment approval |

No connected environment values or credentials were read or created. The key and
known-host secret names follow sibling workflows. Siblings hardcode multiple hosts;
this template deliberately requires the single-host variable instead. If an
existing environment uses a different host-variable name, reconcile the mapping
before dispatch; do not silently pick a sibling or London address.

Before each update, pause every producer/admin trigger, wait for active runs to
finish using their run IDs, make and verify a SQLite-consistent backup, and review
schema compatibility with the previous release. The `runs_drained` input is an
operator assertion, not an invented application drain endpoint. Keep producers
paused until post-release checks pass. Do not authorize automated code rollback
for a backward-incompatible state migration; use a separately reviewed maintenance
plan instead.

The release scripts first perform a read-only remote bootstrap preflight. They
require the root-owned marker, private `.env`, existing healthy unit, a real
`current` release, and writable external `state.db`. They upload only runtime
`app/`, deployment templates and locked Python manifests into a fresh release;
there is no `rsync --delete`, `.env` upload, state copy or live-code overwrite.
Runtime data outside `app/` needs an explicit reviewed allowlist change; never
remove state exclusions as a shortcut.

Dependencies are installed before stopping the old app. Promotion takes a local
deployment lock, requires the installed app unit to exactly match the template
(and have no drop-ins), stops the old process, changes the code symlink and starts
one worker. Bounded checks cover both health routes. On promotion failure, it
attempts to restore the old code pointer and health, without restoring/deleting
SQLite. The workflow still fails after successful rollback. Releases are retained;
there is no automatic cleanup. Updates never install/enable units, reload Caddy,
restart collectors, provision secrets, change DNS or activate producers.

The host needs narrowly scoped noninteractive permission to stop/start only
`cosmofy-warmer.service`. No blanket passwordless shell/sudo permission is required.
An SSH disconnect or runner cancellation is not a transaction guarantee: inspect
the unit, `current`, any known `current.next` staging symlink and stored run states
before retrying. Never recursively delete state or blindly overwrite the database.
If rollback cannot recover health, leave producers paused and involve the operator.

## Optional private observability

Application telemetry is controlled by standard `OTEL_*` settings, not a mandatory
collector dependency. The template defaults to `OTEL_SDK_DISABLED=true` with
exporters `none`; structured stdout/stderr still goes to journald. Once the private
collector is validated, opt into tracing with `OTEL_SDK_DISABLED=false`,
`OTEL_TRACES_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, and a
verified `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` ending in `/v1/traces`. The sample
`25318` endpoint remains an unverified placeholder until the host is checked.
Leave OTLP log/metric exporters disabled for this collector configuration.

`otel-collector.yaml` receives traces over loopback HTTP and reads only
`cosmofy-warmer.service` from journald. It keeps a persistent cursor/export queue,
unwraps the journal `MESSAGE`, sends traces to X-Ray and raw JSON logs to
`/cosmofy/warmer/production` in CloudWatch, with an explicit instance-named stream.
App and collector `OTEL_NODE_NAME` must agree. JSON correlation fields such as
`trace_id`, `span_id`, `request_id`, event, severity and duration remain in the body.
Verify W3C context continuation and redaction against core implementation.
CloudWatch is retained as the baseline log destination; infrastructure owners must
approve log-group retention, least-privilege permissions and credential delivery.
AWS credentials belong only to the collector's private infrastructure-managed
credential chain, never the app, frontend, GitHub test environment or repository.
See the [CloudWatch exporter configuration](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/exporter/awscloudwatchlogsexporter).

`otel-collector-loki.yaml` is an optional additive overlay for the existing shared
Loki, not a new stack. After separate approval, install it as a second config file
and review a collector-unit override that loads both files in this order:

```text
/usr/bin/otelcol-contrib --config=/etc/otelcol-contrib/warmer.yaml --config=/etc/otelcol-contrib/warmer-loki.yaml
```

Validate that exact merged configuration before restarting anything. The new
`logs/loki` pipeline adds a separately bounded exporter queue without replacing the
CloudWatch `logs` pipeline or X-Ray traces. Confirm behavior during a Loki outage;
queues are bounded, not a zero-loss guarantee. Define `LOKI_OTLP_ENDPOINT` only in
the collector environment, pointing to the approved private shared Loki `/otlp`
base URL. Verify network reachability, TLS/auth/tenant requirements and structured
metadata support with the monitoring owner. No endpoint/credentials or auth scheme
are assumed here. If auth is needed, add its reviewed collector-only configuration
before enabling the overlay. Keep trace/run/request IDs out of Loki index labels.
See [Grafana's native OTLP ingestion guidance](https://grafana.com/docs/loki/latest/send-data/otel/).

Collector health only proves the collector process is serving; verify actual
export in CloudWatch, X-Ray and optional Loki separately. Do not open OTLP, collector
health or shared Loki ingestion publicly. Do not modify sibling collectors or the
shared Grafana/Loki infrastructure during Warmer preparation.

## Future daily scheduling — design only

No schedule, Lambda, EventBridge connection, API Destination, rule, IAM policy or
DNS entry is created or activated here. Select and approve the integration first.
Use one logical producer for `rate(1 day)`, not one producer per machine and not
APOD's duplicated seasonal UTC rules. A rate of one day is a 24-hour interval,
not a local-midnight guarantee. Choose an explicit future start date and keep any
later-created schedule disabled until activation is separately approved; omitting
a start date can cause immediate invocation when enabled. See
[Scheduler schedule types](https://docs.aws.amazon.com/scheduler/latest/UserGuide/schedule-types.html).

Two supported integration designs:

1. `Scheduler rate(1 day) -> Lambda HTTP adapter -> Warmer POST -> 202`.
   The schedule targets Lambda's AWS ARN. The adapter fixes an approved HTTPS
   base URL and allowlisted operation, retrieves the bearer token through its
   own managed secret access, uses bounded HTTP timeouts, and records the returned
   run ID durably. No token/URL/query/IP comes from an untrusted event payload.
   A separate bounded monitor follows `GET /runs/{id}` to terminal status; a
   successful Lambda invocation/HTTP acceptance alone is not job success.
2. `Scheduler rate(1 day) -> EventBridge PutEvents -> event-bus rule -> API Destination -> Warmer POST -> 202`.
   Scheduler targets the event bus via `PutEvents`, not the URL. The rule matches
   the intended source/detail type, transforms input to the core's supported body,
   and selects an approved fixed HTTPS API Destination. Its Connection supplies
   `Authorization: Bearer ...` using securely stored credentials. Validate delivery
   of that exact header without logging its value and configure least-privilege
   target roles. It does not automatically follow run status or sequence jobs.

Scheduler supports AWS API targets; it has no direct arbitrary-URL target. Lambda
Invoke and EventBridge PutEvents are documented
[templated targets](https://docs.aws.amazon.com/scheduler/latest/UserGuide/managing-targets-templated.html).

API Destinations have a **5-second maximum client execution timeout**. Both POST
routes must commit accepted work and return `202` plus a durable run ID comfortably
within that budget; never hold the HTTP request open for warming/discovery. A
`202` prevents delivery retry but proves only acceptance. Configure bounded retries
and a DLQ; API Destinations retry timeouts and selected statuses including `409`,
`429` and `5xx`, so a lost response can redeliver already-accepted work. See
[API Destination timeout and retry behavior](https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-api-destinations.html).

Before activation, prove durable deduplication of repeated delivery, or implement
it in the adapter/orchestration layer with persisted event-to-run mapping. No
idempotency header is claimed in the current HTTP contract. A single-active-run
guard alone does not deduplicate sequential retries. Define failure alarms, DLQ
review and a monitor for accepted-but-failed/stale/partial runs.

Do not fan out discovery, `warm/articles` and `warm/picture` simultaneously: the
service permits only one active operation. If the daily workflow needs all three,
use durable serial orchestration that waits for each terminal run result before
the next POST. An API Destination is not that orchestrator; choose the adapter
plus a durable monitoring/continuation design when sequencing is required. A
simple one-operation daily trigger may use the rule/API Destination path once
deduplication and completion monitoring are verified. The main build owns those
functions; this handoff intentionally contains no Lambda/business implementation.

Require an authorized staging acceptance check: fast `202`, persisted run ID,
authenticated polling, honest full/partial coverage, duplicate-delivery handling,
restart recovery, bounded retries and observable failures. Verify AWS-to-OCI HTTPS
reachability and approved ingress without assuming a Lambda/API Destination can
reach a Tailscale-only address. Do not weaken the network boundary merely to make
scheduling work. Enabling the chosen producer is a separate approval, not a CI step.

## Verification status

Local checks passed: nine deployment safety tests (including twelve pytest subtests)
under Python 3.14 with `uv run --locked pytest -v deploy/tests`, with a generated
and parsed JUnit XML report; Bash syntax checks; actionlint workflow validation;
ShellCheck script checks; YAML parsing and additive CloudWatch/Loki pipeline checks.
The report was generated in a task-specific temporary directory, not committed.
The workflow itself discovers from the repository root and uses the matching
`test-results/pytest.xml` output/upload path. Deployment tests are collected by the
current root pytest configuration; no nonexistent test directory is referenced.

Linux systemd/Caddy/collector
runtime validation, remote port checks, host bootstrap, GitHub execution, external
telemetry receipt and EventBridge delivery remain unperformed. The main build's
application test result is separate from deployment-template validation. No
production availability or deployment success is implied by these files.
