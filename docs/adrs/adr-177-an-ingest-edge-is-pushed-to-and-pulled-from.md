# ADR-177: An ingest edge is pushed to and pulled from

**Status:** Proposed
**Date:** 2026-10-07
**Related:** ADR-106 (push ingest gateway), ADR-157 (serving budgets), ADR-164 (node functions, draft), spec-signal-ingest-and-producer §5–6.1, spec-serve §10


**2026-10-08 (ADR-180):** the outbox defined here is reused on the producing side. A site's local medallion keeps the same outbox, and its forwarder ships from it to an upstream intake, which may be this edge. Idempotency on (source, content_hash) is what lets two transports deliver a batch once.

## Context

ADR-106 gave the data platform one front door for producers: `POST /ingest`
for documents and `POST /ingest/rows` for rows, authenticated, with the site
taken from the credential and at-least-once delivery made idempotent by
content hash. It assumed producers can reach the node that owns the data
platform.

That assumption fails in a common arrangement:

- The node that owns the data platform sits on an institutional network. Its
  name resolves to a private address even in public DNS, so nothing outside
  can open a connection to it.
- Producers belong to other institutions, whose security offices allow only
  connections that start on their side, outbound, over HTTPS. Nothing may
  connect into their networks, and they will not hold accounts on ours.

Neither side may call the other. Today the gap is bridged by a shared file
service: producers deposit files, and the owning node pulls them on a timer.
That works and needs no new approvals, but it delays data by up to a day,
moves whole files rather than readings, and gives a producer no answer to
"did my rows land?".

## Decision

A third node, the **ingest edge**, sits where both sides may reach it.
Producers push to it; the owning node pulls from it. Every connection is opened
by the side that is allowed to open one.

1. **The edge is a function profile, composed from an allow-list.**
   `axi serve --profile ingest-edge` serves `/ingest` (both lanes, unchanged),
   `/edge` (the export) and `/healthz`, and nothing else. Mounts declare the
   node functions they serve (`MountSpec.functions`); a function profile serves
   only mounts that declare one of its functions. This is the opposite of the
   existing `profiles` filter, where an empty list means everywhere, and it has
   to be: a node exposed to the internet must not gain a mount because someone
   added one next month. The edge has no web application, chat, retrieval,
   data serving or database.

2. **Producers see the same front door.** Tenancy, bounds and admission are
   ADR-106's and ADR-157's: the site comes from the credential, a batch naming
   another site is refused, request shape is bounded, and each site has a burst
   and a sustained rate. A producer configured for the edge differs from one
   configured for the owning node only in its URL.

3. **The outbox is the edge's record of what it holds.** After a batch is
   durable in bronze and not excluded, the edge appends one JSON line: a
   sequence number, the source, the batch's identity and declared schema, its
   tenancy metadata and the SHA-256 of its payload. The outbox is idempotent by
   `(source, content_hash)`. A producer's replay re-reports a batch, which is
   what repairs an outbox write that failed after the bronze write succeeded.

4. **The owning node pulls, exactly once into its bronze.** It pages
   `GET /edge/outbox?after=<seq>`, fetches each batch with
   `GET /edge/content/<source>/<sha256>`, refuses any payload whose hash does
   not match, and lands the rows through its own row sink under the local
   connector of the same name (or as mapped). The cursor advances only after a
   batch is durable locally, so an interrupted pull resumes; row-hash dedup
   makes a redelivered batch land nothing. Exactly-once therefore needs no
   coordination with the edge and no acknowledgement protocol. The pull is the
   `edge` connector kind and `axi data edge-pull <connector>`, run on a timer.

5. **The export is one-way and names its reader.** Only principals listed in
   `AXIOM_EDGE_DOWNSTREAM` may read `/edge`, in addition to the
   `edge_export:read` scope. A scope alone would let a wildcard read grant
   export every producer's data; naming the downstream makes that impossible.
   Unset exports to nobody.

6. **Public means no anonymous convenience.** The edge binds loopback and sits
   behind a TLS proxy, so every request arrives from 127.0.0.1. The default
   posture's loopback convenience (credential-less access for a developer's
   local node) never applies to a function-profile node. Building this found
   the hole: an anonymous push landed on a loopback-bound edge.

7. **Deterministic gates, no governance database.** The production governance
   engine graduates new action classes through approvals kept in Postgres, and
   an edge holds no database by design. The edge's gates are deterministic
   instead: issued keys with scopes, the credential-derived site, request
   bounds and rates, the downstream allow-list, and no anonymous access. The
   owning node, which has the database, keeps the full engine.

## Keys

- **Producers:** one key per site, bound to that site, with the push scope
  only. Issued on the edge (`axi gate issue api-key --site <site>`), handed
  over directly, stored in the producer's vault.
- **Downstream:** one key, unbound to any producing site, with
  `edge_export:read`, stored in the owning node's vault and referenced by the
  `edge` connector's `credential_ref`.
- **Rotation:** issue the replacement, hand it over, then revoke the old key;
  revocation takes effect on the next request. The edge serves no sign-in, so
  invitation redemption is not available on it. Keys are delivered directly
  until a node that serves sign-in can mint them for the edge.

## Retention and outages

- The edge keeps every batch until the downstream has pulled it, plus a
  retention window (30 days proposed). Expiry is a later step and must refuse
  to delete anything past the downstream's cursor.
- While the downstream is offline, producers are unaffected and the outbox
  grows. Disk is sized for a month of producer peak plus the window.
- While the edge is offline, producers spool locally and replay (ADR-106);
  the replay is idempotent at the edge and again at the downstream.

## Audit

Every push is logged with the key that made it, and the outbox records what
arrived and when. The downstream's bronze keeps each batch's identity and
tenancy metadata, so a row can be traced from silver back to the edge record
and the key that sent it.

## Deployment

The first deployment shares a public web host that already serves another
site behind nginx, rather than a dedicated machine. The edge runs there as a
container published only on the host's loopback, and the site's existing
server block gains one `location` for `/ingest` and `/edge`, placed before
its catch-all, so every other path keeps its current owner. nginx stays the
only process on 443; the edge never terminates TLS or faces the internet
directly, and the "no anonymous access behind a loopback proxy" rule above is
what makes that safe. The package for this shape is in
`deploy/ingest-edge/`: the container and its compose file, the one-block
nginx include, a read-only preflight, and a no-root variant (a per-user
service, with a fallback that does not survive a reboot) for hosts where the
operator has neither root nor Docker. A dedicated host with the shipped
Caddy configuration remains the default when one is available. The deployment
is declared live only after a probe from outside the owner's network passes
(the `deployed` job of the ingest-edge smoke workflow).

## Consequences

- Producers on networks that allow only outbound HTTPS can send live data to
  a platform on a private network with no inbound exception on either side.
- One more node to run. It is small, holds only bronze, and is rebuilt from a
  tagged release and its env file.
- Delay is the pull interval rather than a daily file sweep. A push
  notification to the downstream is possible later, but would need an inbound
  path to the downstream and is out of scope.
- `MountSpec.functions` is the first concrete use of node functions (ADR-164
  draft). Other function profiles can follow the same allow-list rule.
- The document lane (`/ingest`) lands on the edge but is not yet in the
  outbox; only row batches are exported in this cut. A producer's
  "did it land?" (`GET /ingest/summary`) is answered by the owning node, not
  the edge, which holds no database.

## Alternatives considered

- **Expose the owning node.** A public address or proxy for the owning node
  needs an institutional security exception and exposes far more than ingest.
- **An outbound tunnel from the owning node.** Bypasses the institutional
  firewall's intent and would need explicit sign-off.
- **Keep the shared file service.** Retained as the bulk path for large raw
  files, and as the interim path until an edge is provisioned.
- **The edge pushes to the owning node.** Impossible: nothing may connect in.
