# Remaining activation work

- Verify full live POP coverage for the current Stellate hostname; resolve every missing mapping before calling a warming run globally complete.
- Bootstrap one approved Oracle instance, domain/TLS, private administration access and persistent state. Deployment templates do not mean this is deployed.
- Activate and verify the dedicated private collector with AWS CloudWatch/X-Ray and supplementary Loki; do not overwrite existing collectors.
- Configure the daily EventBridge trigger and a job-result monitor after deployment. HTTP202 only acknowledges acceptance.
- Coordinate Java administrative mutation and app query selections using docs/GRAPHQL_INTEGRATION.md.
- Define content freshness/purge coordination separately. Daily warming cannot override an existing five-minute edge TTL or make eviction impossible.
- If multiple replicas are required later, replace single-instance SQLite ownership with coordinated shared job state/locking before putting replicas behind a load balancer.
- Optional: explicit second-request HIT verification, idempotency keys for scheduler redelivery, configurable audited inventory retirement and bounded run-history retention.
