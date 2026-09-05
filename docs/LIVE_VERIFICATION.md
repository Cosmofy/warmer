# Live verification — 5 September 2026

The implementation is committed and pushed to Cosmofy/warmer. The initial
[GitHub test workflow passed](https://github.com/Cosmofy/warmer/actions/runs/33957601459)
with 195 tests and 12 subtests. Local tests also passed with warnings treated as
errors. Production deployment, DNS, collectors and EventBridge have not been activated.

## Actual discovery

Executed from the development Mac against `https://livia.stellate.sh`, not from
an assumed production Oracle deployment. The final discovery completed at about
09:16 UTC in **26.57 seconds**, with **164 targets, 30 mapped POPs and 134 gaps**.
It is explicitly **incomplete**. Thirty POPs do not satisfy the all-POP requirement.

The source check found a real incompatibility: Fastly's negotiated markdown returned
only 14 metro-site rows, whereas explicit HTML returned 164 complete-table codes.
The request now asks for HTML; the parser also rejects the identified metro-only
table. The earlier 14-target diagnostic is retained in local history but is
superseded and must not be used as the current coverage denominator.

136 DNS probes were accepted in the final discovery. All 38 candidate-address
verification requests succeeded, resolving to 30 distinct POPs. Globalping rate
limiting/quota, DNS failures, and unavailable probe-city names affecting 51 targets
prevented full discovery. Some unavailable cities were reached through other cities'
DNS answers; city availability and actual POP coverage are different measurements.

No subnet scanning, cache purge, purchases, new probe machines, or quota-bypass
attempts were made. Additional Globalping allowance alone is not proof that the
unavailable cities or every target can be covered.

## Real named warming jobs

The actual `Jobs` manager and `EdgeClient` executed the registered full Articles
query twice using the persisted inventory. Missing targets stayed in each run.

| Run | Targets | Covered | Missing | Actual requests | Successful HIT / MISS | Duration | Status |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| First | 164 | 29 | 135 | 30 | 2 HIT / 27 MISS | 7.03 s | incomplete |
| Second | 164 | 30 | 134 | 30 | 29 HIT / 1 MISS | 4.50 s | incomplete |

First run ID: `eef26fcb315549e1b9ab109649077a84`.
Second run ID: `4b716591d0a14d65b0dd1446564ab8ff`.

On the first pass an address previously verified as SIN routed to NRT. The service
reported `pop_mismatch` and did not credit SIN. It returned SIN on the second pass,
with MISS. This demonstrates that a verified IP is not a permanent one-to-one POP
guarantee; every warming response still needs the actual POP check.

A separate London Articles smoke returned MISS then HIT. An old BOM candidate
returned QBD and was correctly rejected when BOM was the expected target.
The registered `picture` query also returned HTTP200, a valid current Mountain Time
APOD date, actual POP LHR, and MISS in **1.22 seconds**. This is one-POP APOD
verification, not a claim that APOD was tested at every POP.

## Evidence and remaining work

Local runtime evidence is in ignored `data/live-discovery.db`, including the
inventory, mapping timestamps, exact target gaps and persisted run results. It is
not committed as production state. Every subsequent discovery must freshly verify
addresses from its actual egress location.

The unresolved requirement is full network coverage: obtain sufficient approved
DNS-probe allowance and investigate candidates for missing locations; verify each
against the live service. If regional egress or a provider-supported targeting
mechanism is necessary, select that explicitly. Do not relabel 30/164 as complete
or remove unreachable POPs to manufacture success.

After coverage/infrastructure choices, bootstrap one service instance, activate
private AWS/Loki collection, connect Java's administrative mutation, and configure
EventBridge plus terminal-job monitoring. The repository contains instructions and
templates for those steps, not an active production deployment.
