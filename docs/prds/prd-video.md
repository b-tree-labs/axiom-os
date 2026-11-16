# Product Requirements: Live Video

**Product / Feature:** `video` extension — video sources (RTSP first) and stream triggers

**Owner:** B-Tree Labs   •   **Status:** Draft   •   **Last updated:** 2026-11-11
**Spec:** [spec-video.md](../specs/spec-video.md) • **ADR:** [ADR-240](../adrs/adr-240-a-video-source-is-a-connector-with-a-kind-and-a-trigger-is-a-registered-contract.md), [ADR-256](../adrs/adr-256-an-instrument-quantifies-what-a-sensor-gives-and-kinds-register-under-one-noun.md) (video is the first instrument kind)

---

## 1) Elevator pitch

An Axiom node on an edge machine holds video sources — first the live RTSP streams
of the IP cameras it is connected to — and emits events when something happens in
them, so a domain consumer can process video by registering a trigger instead of
building a capture stack.

## 2) Problem

- Cameras are the one sensor an edge node commonly has that the platform cannot
  declare, hold, or observe; and a camera is not the only thing that yields frames
  (a saved recording does too), so the seam must be a *source*, not a protocol.
- Every consumer that wants live video would otherwise write its own RTSP session,
  reconnect loop, credential handling and health reporting, and each would open the
  camera again.
- Interpretation of what a camera sees is domain work and must not land here
  (AGENTS.md, *Which layer does this belong to?*).

## 3) Goals

- A video source is declared, verified and observed like every other connector; its
  kind says what it is, and RTSP is the first kind.
- A source is held once and shared; triggers do not know which kind feeds them.
- A consumer adds processing with no core change.
- The platform ships one generic trigger, motion, that is useful on its own.

## 4) Users

- **Site operator** (not a programmer): adds a source, confirms it works, sees it
  is alive, sees what fired.
- **Consumer developer**: implements a `StreamTrigger`, registers it, configures it
  on a stream.

## 5) Scope

### Phase 1 — capture

| ID | Requirement |
|---|---|
| VID-001 | A video source is a connector of kind `video` whose `source.kind` names what it is; the extension keeps a source-kind registry, and a kind declares whether it is ephemeral or replayable. |
| VID-002 | `rtsp` is the first source kind: a URL and a `credential_ref`; a URL carrying userinfo is refused with the vault instruction. |
| VID-003 | `verify` produces one frame and reports codec, resolution and frame rate; the connector is inactive until it passes. |
| VID-004 | The capture service holds one session per source, decodes out of process, and fans frames out through a bounded in-process bus. |
| VID-005 | A lost ephemeral session reconnects with backoff and jitter; counters record frames, drops, reconnects and last-frame age. |
| VID-006 | `status` reports every source's session state and counters; no output contains a credential. |
| VID-007 | Two copies of the service never hold one source at once; a switch is by overlap (ADR-182). |
| VID-008 | `diagnose` names the exact fix for a missing `ffmpeg`, an unreachable URL, failed authentication or an unsupported codec. |
| VID-009 | Once started, a source is captured whether or not any trigger is attached. |

### Phase 2 — triggers

| ID | Requirement |
|---|---|
| VID-010 | `StreamTrigger` and `TriggerEvent` are the only seam between a source and processing; a trigger sees frames and never the source kind; it is discovered by entry point or manifest block and gated like other discovered code. |
| VID-011 | An event carries a stable incident key; repeats of one episode produce `started` and `ended`, not a stream of repeats. |
| VID-012 | Every event is appended to a local ledger; `axi instrument video events [<source>]` lists them. This is the end-to-end proof: motion events from a connected IP camera, listed. |
| VID-013 | An event carries a severity from the trigger's own configuration (default `info`); the trigger never names a recipient. |
| VID-014 | `motion` ships as the first trigger: frame differencing over an optional region, with per-source thresholds, and says it is a heuristic. |
| VID-015 | A trigger is attached by configuration and `axi connector reload`, not by a new verb under `axi instrument video`. |

Out of scope: browser or any playback surface, recording, storage of frames,
domain-specific triggers, any source kind other than `rtsp`.

## 6) Constraints

- Secrets: vault only (`credential_ref`); redacted everywhere.
- Dependencies: `ffmpeg` on the machine; optional extra `video = ["numpy"]`. No
  OpenCV.
- Video is instrument kind `video` ([prd-instrument](prd-instrument.md), ADR-256): its
  verbs are `<cli> instrument video <verb>`, and the `instrument` noun carries node
  function `acquire` (ADR-164). Skill names stay `video.<verb>`.
- Every CLI verb is a skill function (ADR-056); `axi ext lint` passes.

## 7) Phases

- Phase 1: capture — VID-001..009.
- Phase 2: triggers — VID-010..015.

## 8) Acceptance

- Phase 1: a simulated camera (`ffmpeg` `testsrc` over RTSP) is added, verified,
  captured and observed through the CLI; the unit and integration tests in
  spec-video §Tests are green.
- Phase 2: `motion` opens and closes one episode on a synthetic moving object and
  stays silent on static noise; the episode appears in the ledger with one incident
  key.
