# ADR-147: Surfaces receive live updates over server-sent events

**Status:** Accepted (2026-09-30)
**Related:** [ADR-123](adr-123-receipts-surface-architecture.md) (appkit shell),
[ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md),
[spec-event-bus.md](../specs/spec-event-bus.md)

## Context

Books used at consoles need sub-second propagation: a record signed at one
console, an obligation countdown, a missed check, or a pending co-signature
must appear on every console at the site. Consumer operations and kiosk views
need the same.

The platform has no WebSocket server anywhere. Server-sent events exist in two
places, chat streaming and the agent mount. appkit has no live hook: its
`useResource` refetches stale data, and its chat engine reads a fetch stream.
The event bus has NATS-shaped subjects but only an in-process transport.

## Decision

- Surfaces receive live updates over **SSE**. Axiom provides one helper,
  `axiom.http.sse`, that turns a bus subject pattern plus an authorisation
  filter into an SSE endpoint. It provides:
  - resumable streams (`Last-Event-ID` backed by the publisher's outbox id);
  - heartbeats every 10 s;
  - per-principal filtering applied on the server.
- appkit provides `useLiveStream`: reconnect with resume, heartbeat
  supervision, a **degraded** state after 25 s without a heartbeat, and a
  `LiveIndicator`. Degraded is always shown, so silence never reads as healthy.
- Client-to-server traffic stays ordinary HTTP. Presence and typing hints are
  small POSTs.
- Domain events reach the bus through a transactional outbox in the
  publishing extension, so a crash between commit and publish loses nothing.

## Options considered

- **WebSocket.** Lost: it adds a second server protocol and a proxy
  configuration for a use that is overwhelmingly server-to-client. SSE works
  through the existing webgate and TLS front door.
- **Polling via `useResource`.** Lost: it cannot meet sub-second propagation
  without load that grows with consoles × views.

## Consequences

- One live-update path for every extension and consumer surface.
- The in-process bus limits SSE fan-out to one node process. A multi-process
  node needs the bus's network transport first. The helper's interface does
  not change when that lands.
