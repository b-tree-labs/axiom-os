# Product Requirements: Live Video

**Product / Feature:** `video` extension — video sources (RTSP first) and stream triggers

**Owner:** B-Tree Labs   •   **Status:** Draft   •   **Last updated:** 2026-11-16
**Spec:** [spec-video.md](../specs/spec-video.md) • **ADR:** [ADR-240](../adrs/adr-240-a-video-source-is-a-connector-with-a-kind-and-a-trigger-is-a-registered-contract.md), [ADR-264](../adrs/adr-264-a-video-trigger-can-take-the-events-of-the-triggers-before-it-on-its-source.md) (a trigger takes another's events), [ADR-256](../adrs/adr-256-an-instrument-quantifies-what-a-sensor-gives-and-kinds-register-under-one-noun.md) (video is the first instrument kind)

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
- The platform ships two generic triggers, each useful on its own: `motion` (did
  something move) and `reference-match` (which of a set of named reference pictures
  does a part of the view match, or none). Neither knows what is in view; a trigger
  that does is domain work and stays in its consumer.

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

### Phase 3 — matching a region against references

| ID | Requirement |
|---|---|
| VID-016 | `reference-match` ships as the second built-in trigger: it reads which of a set of named reference pictures each configured region of the view matches, or none, and says it is a heuristic. It knows nothing about what is in view: the regions, the reference pictures, their labels and the thresholds come from a reference set the consumer builds. |
| VID-017 | A reference set is a directory named by an absolute path: a manifest with a format version, a name and a version, and reference pictures that numpy loads without pickle, each checked against the hash the manifest records. A set that cannot work as written, or an attachment setting the trigger does not have, is refused when the attachment is built, with its fix. |
| VID-018 | A region reads a label only while the view, the exposure and the region's own contrast pass their guards, and only when its best reference clears that reference's minimum match and beats the runner-up by the region's margin. Otherwise it reads `unknown`, with the reason. `changing` and `unknown` are the trigger's own, and no set may declare them. A frame whose size the set does not fit is a configuration fault, not `unknown`. |
| VID-019 | A label is reported only after it has held for a dwell time, and a reported label is left after a time without agreement, both on the frames' own clock. One change is one episode: `started` when the reported label is left, `ended` under the same incident key when the next one is reached. A return to the same label is flagged and is not a change. |
| VID-020 | While frames arrive, the trigger is never silent for longer than its heartbeat, in every state, `unknown` included. Every event names its region, the reference set and its version; no number is called a confidence. |
| VID-021 | Several regions ride one attachment and report independently. `status` shows each region's state, since when, its last scores and its counts. |

### Phase 4 — the event journal

The ledger becomes the data platform's DAQ journal, one per source, so an event is
stored the way a reading is and can later be sent the way a reading is.

| ID | Requirement |
|---|---|
| VID-022 | Every event is appended to its source's own journal, `<state>/video/journal/<source>/`: the data platform's DAQ journal, written through its consolidator, one feed per trigger. Nothing writes `events.jsonl` any more. |
| VID-023 | A journaled event is a signal record: a gapless `seq` and a hash chain per feed that `verify_chain` finds no fault in, continued across a restart and a change of holder, and the trigger named as what produced it (`source_class = estimated`, `model_ref = <name>@<version>`). |
| VID-024 | One writer at a time per source journal, across threads and processes: triggers on one source, the capture service closing an episode, a successor taking the source over and `revoke` never write one `seq` twice or break the chain. A last line cut short by a power loss is skipped when read and never stops the next write. |
| VID-025 | `instrument video events` reads the journals, every source's or one, oldest first, each row exactly the event's fields as before. Events an earlier release wrote to `events.jsonl` are still listed, until a later release retires that file. `revoke` keeps the journal. |
| VID-026 | Nothing is sent and nothing is deleted: the journal has no consumer yet, so it keeps every event. Sending later is a transmitter cursor on the same journal, not a change to how events are written. |

### Phase 5 — triggers on triggers

A consumer's processing is a series of small triggers, each taking the events of the
one before it on the same source: motion, then what is visible, then what each
visible part holds. Each keeps its own version, feed and health (ADR-264).

| ID | Requirement |
|---|---|
| VID-027 | An attachment names with `on` the attachments on its source whose events it takes, every kind or one (`<name>:<kind>`). The trigger is given a copy of each event they write, after it is written and in the order written, through `on_event`, on its own thread and never at the same time as `observe`. What `on_event` returns is journaled like any event and can be taken further on. The factory never sees `on`. |
| VID-028 | A trigger is given its events and frames in frame time: an event comes before the first frame at or after the event's time. With no frame to wait for, or frames behind it, an event is not held longer than a second. |
| VID-029 | An `on` that cannot work is refused with its fix: a malformed entry, a kind that is not one, an attachment not on this source, the attachment itself, a cycle, or a trigger that does not take events. A reload with one changes nothing. |
| VID-030 | A trigger behind on its events drops its oldest and counts them; nothing upstream waits. `status` shows what each trigger is on and how many events it was given and dropped. |

Out of scope: browser or any playback surface, recording, storage of frames,
domain-specific triggers (what a region shows, which labels exist and the reference
pictures are the consumer's), building, choosing or validating a reference set (the
platform reads one; the consumer builds it), any source kind other than `rtsp`.

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
- Phase 3: reference match — VID-016..021.

- Phase 4: the event journal — VID-022..026.
- Phase 5: triggers on triggers — VID-027..030.

## 8) Acceptance

- Phase 1: a simulated camera (`ffmpeg` `testsrc` over RTSP) is added, verified,
  captured and observed through the CLI; the unit and integration tests in
  spec-video §Tests are green.
- Phase 2: `motion` opens and closes one episode on a synthetic moving object and
  stays silent on static noise; the episode appears in the ledger with one incident
  key.
- Phase 3: on synthetic scenes, `reference-match` reads the label of the reference a
  region matches and `unknown` with its reason on a shifted view, bad exposure, a flat
  region or a match to no reference; a change of light alone never gives another label;
  one change is one `started`/`ended` pair; a heartbeat arrives in every state; two
  regions in one attachment report independently; every refusal names its fix. On a
  simulated camera, a door that opens and closes is reported as one change per
  opening or closing, each under one incident key, and is listed by
  `instrument video events`.

- Phase 4: motion events from a started source land in that source's DAQ journal,
  one feed per trigger, with an intact chain across a change of holder and no
  `events.jsonl` written; `instrument video events` lists them, and what an earlier
  release wrote, exactly as before.
- Phase 5: a trigger attached `on` motion is given each motion event in frame time
  with its frames, and a third on it is given what the second wrote; a broken `on`
  is refused at reload with its fix and nothing changes.
