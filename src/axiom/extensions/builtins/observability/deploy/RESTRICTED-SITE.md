# Deploying the observability substrate at a restricted site

Written after checking this chart against what actually ran on the prototype
node. Three things were wrong for a site that cannot reach Docker Hub.

## What was fixed

**Images are repointable.** `global.registry` prefixes every image in the
chart, init containers included. They were unqualified Docker Hub references
and three `busybox:1.36` init containers bypassed values entirely, so the chart
had to be forked to run anywhere without public egress.

**`image.pullSecrets` is now consumed.** It was declared in `values.yaml` and
referenced by no template — an operator could set it against a private registry
and watch every pull fail anyway.

**Tags are pinned to the appVersion.** `Chart.yaml` claimed `3.45.0` while the
image tag was the floating major `3`, so two installs of the "same" chart were
not the same software. Now `langfuse 3.45.0`, `postgres 16.4`,
`clickhouse 24.8`.

## What was MISSING, and matters most

The chart shipped Postgres, ClickHouse, web and worker. Langfuse v3 also
requires:

- **Redis/Valkey** — the queue between web and worker.
- **S3-compatible blob storage** — raw event payloads; ClickHouse holds only
  the queryable rows.

Without them nothing fails loudly: the web pod starts, accepts traces, and the
worker has nothing to consume, so ingestion silently does nothing. The chart
now **refuses to render** without both rather than deploying that.

This was found by comparing the chart against the volumes left on the prototype
node, which include `valkey-data-langfuse-redis-primary-0` and `langfuse-s3`.
Those PVC names are upstream Langfuse chart naming, which means **the thing
that ran there was the upstream chart and this one has never been deployed.**

Neither component is shipped internally yet, so both are `external` and
required. SeaweedFS is the house object store (MinIO is excluded by licence
policy).

## Minimum restricted-site install

```
helm install axiom-observability ./helm \
  --set global.registry=registry.example.internal/axiom \
  --set 'image.pullSecrets={site-registry}' \
  --set redis.external.connectionString=redis://valkey.axiom:6379 \
  --set s3.external.endpoint=http://seaweedfs.axiom:8333 \
  --set s3.external.accessKeyId=... --set s3.external.secretKey=... \
  --set langfuse.salt=... --set langfuse.encryptionKey=... \
  --set langfuse.nextauthSecret=... --set clickhouse.internal.password=...
```

Mirror these into the internal registry first:

```
langfuse/langfuse:3.45.0
clickhouse/clickhouse-server:24.8
postgres:16.4                      # only when postgres.mode=internal
busybox:1.36
```

## Export-control notes

- `TELEMETRY_ENABLED=false` is hardcoded in the chart, not exposed in values,
  so a site cannot switch Langfuse's phone-home on by accident.
- `postgres.mode` defaults to `external` — the shared Axiom OLTP — so a site
  that wants an isolated trace database must set `internal` deliberately.
- Secrets belong in the Axiom vault, not in `--set` on a command line, which
  lands in shell history and the process table. The `--set` form above is shown
  for clarity; use a values file with restrictive permissions, or the vault,
  for anything real.
