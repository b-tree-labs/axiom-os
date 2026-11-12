# ADR-240: A video source is a connector with a kind, held once; a trigger is a registered contract that emits events

**Status:** Accepted (2026-11-09; the owner accepted the ADR, prd-video and spec-video, and asked for Phase 1)
**Related:** ADR-057 (connector as a top-level primitive), ADR-220 (a provider is a connector kind, built by factory from configuration, credential in the vault), ADR-238 D2/D4/D5 (publishers never name people; one incident, one message; one connector lifecycle), ADR-023-A1 (portfolio gate on discovered code), ADR-182 (never down from our own causes; a collector updates by overlap), ADR-164 (node functions), [spec-video](../specs/spec-video.md), [prd-video](../prds/prd-video.md)

## Context

An Axiom node on an edge machine is connected to IP cameras that speak RTSP. The
platform has no way to declare one, hold its stream, or react to what happens in
it. A domain consumer wants to run its own processing on those streams; Axiom must
hold only the generic part: capture, health, and a contract for "something
happened", with motion detection as the first generic implementation.

A camera is not the only thing that yields frames: a saved recording does too, and
the same triggers should run on either. Three things would be expensive to reverse
once a consumer builds on them: how a source is declared and authenticated, how many
sessions a source is asked to serve, and the shape a consumer's processing plugs
into.

## Decision

**1. A video source is a connector of kind `video`, and its `source.kind` names what
it is.** It is declared as configuration in the connector registry (ADR-220 §2),
resolved by name, and its credential, if any, is a `credential_ref` into the vault.
The `video` factory dispatches on `params.source.kind` to a **source-kind registry**
owned by the extension: a `VideoSource` kind declares whether it is *ephemeral* or
*replayable* (SIG-B3), how to `verify` itself, and how to open a session that yields
frames. The first and only kind in this ADR is `rtsp` (ephemeral): a URL without
userinfo (a URL with userinfo is refused, not scrubbed) plus a credential ref. The
connector implements the ADR-238 D5 lifecycle (`detect`, `connect`, `verify`,
`status`, `revoke`) and is not active until `verify` has produced one frame.
`axi instrument video add` writes this connector (video is the first instrument kind,
ADR-256); `axi connector status` lists it with every other connector.

**2. One acquisition per source, decoded out of process.** The capture service opens
one session per source and fans decoded frames out through a bounded in-process
frame bus (drop-oldest, counted). Decoding is an `ffmpeg` subprocess writing raw
frames to a pipe; no video library is imported into the Axiom process. The service
switches by overlap with a durable per-source claim (`axiom.infra.pending_claim`),
so two copies never both hold a source. Once started, a source is captured whether or
not anything consumes its frames.

**3. A trigger is a registered contract that emits events.** `StreamTrigger`
(`start`, `observe(frame) -> TriggerEvent*`, `health`, `stop`) and `TriggerEvent`
(stream, trigger name and version, time, kind, score, detail, stable `incident_key`)
are the only seam between a source and anything that interprets it; a trigger sees
`Frame`s and never knows which source kind produced them. Triggers are
discovered from the entry-point group `axiom.video_triggers` and the manifest block
`[[video_triggers]]`, behind the portfolio gate (ADR-023-A1); built-ins register the
same way, so a consumer's trigger is a package and never a core change. A trigger's
output is appended to a local ledger, carries an incident key, and never names a
recipient (ADR-238 D2); where it goes from the ledger is not decided here. `motion` is the first built-in.

## Options considered

- **A source as a `video` configuration file of its own, outside the connector
  registry.** A second registry with its own credential handling and no shared
  `status`. Rejected: ADR-220 exists so that core code names a connector, never a
  kind; ADR-238 exists so every connector is set up and verified the same way.
- **One connector kind per source kind (`rtsp`, later others) with no family.**
  Follows ADR-220 most literally. Rejected: `axi instrument video list` and the trigger
  runner would need a second list of "which connector kinds are video", and the shared
  behaviour (bus, claim, triggers, status) would be repeated per kind. One `video`
  kind holds that once; the source-kind registry holds only what differs.
- **Decode with OpenCV or PyAV in-process.** Simpler API. Rejected: a decoder bug or
  a codec hang takes the service with it, and the dependency pins a native wheel to
  every install. A subprocess is killed, restarted and versioned independently.
- **One session per consumer** (each trigger opens the camera). Simplest to write.
  Rejected: cameras cap concurrent clients, and the second client doubles the
  camera's load; the frame bus costs one queue.
- **Triggers as plain callbacks registered in code.** Rejected: a consumer could not
  add one without editing Axiom, and an unreviewed callable would run outside the
  gate every other discovered extension passes.
- **Triggers address a recipient directly.** Rejected by ADR-238 D2: routing is a
  person's decision, and the first deployment would hard-code who is on duty.

## Consequences

- A consumer adds processing by shipping a package with one entry point, declaring a
  trigger in a connector file, and running `axi connector reload`.
- Every source is in one place for credentials, health and reconnect, and a new
  source kind is one registration that gets triggers for free.
- `rtsp` is the only kind decided here. Adding a kind is a spec change, not a new
  ADR, unless it breaks the `VideoSource` seam.
- `ffmpeg` becomes a system dependency of any node that captures; `axi instrument
  video diagnose` and `node doctor` report its absence with the fix.
- The ledger is the record. Routing an event to a person (ADR-238) or persisting it
  beyond the node is left for when it is needed; the trigger's contract does not
  change when that happens.
- Follow-ups: `prd-video.md` (requirements), `spec-video.md` (contracts), the `video`
  extension; a worked example for AGENTS.md on the feature/interpretation boundary
  once the first consumer trigger exists.
