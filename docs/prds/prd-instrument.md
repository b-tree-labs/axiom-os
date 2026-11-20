# Product Requirements: Instruments

**Product / Feature:** `instrument`: one command noun for everything a node quantifies from its sensors

**Owner:** B-Tree Labs   •   **Status:** Draft   •   **Last updated:** 2026-11-11
**Spec:** [spec-instrument.md](../specs/spec-instrument.md) • **ADR:** [ADR-256](../adrs/adr-256-an-instrument-quantifies-what-a-sensor-gives-and-kinds-register-under-one-noun.md)

---

## 1) Elevator pitch

An operator reaches everything their node takes in from the physical world under one
noun, `instrument`, and a developer adds a new kind of instrument by shipping a package,
the same way the platform adds its own.

## 2) Problem

- Each thing a node takes in would otherwise bring its own command noun (`video` first,
  then the next), and an operator would have to learn which noun holds which thing.
- A consumer that wants to add an instrument kind has no door: a noun has one owner,
  and a consumer package that claimed one would hide the platform's without an error.
- A camera and a temperature probe produce different data that travel different paths.
  What they can share is how a person finds, lists and checks them, not how their data
  moves.

## 3) Goals

- One noun lists and checks every instrument on a node, whatever its kind.
- A kind is added with no core change, and Axiom's own kinds use the same door.
- Each kind keeps its own verbs, data path and dependencies; the noun stays thin.
- Help, Tab completion and the `next:` lines reach a kind's verbs as they reach any other.

## 4) Users

- **Site operator** (not a programmer): sees what the node takes in, whether each
  instrument is healthy, and reaches the kind's own verbs.
- **Consumer developer**: ships an instrument kind as a package.
- **Agent**: lists instruments and their status through the same skills the CLI uses.

## 5) Scope

Words (ADR-256 D1): a **sensor** is the physical device; an **instrument** is the part
on the node that turns a sensor's output into quantified data (a frame, a reading with a
unit); an **instrument kind** is defined by what it quantifies into.

| ID | Requirement |
|---|---|
| INS-001 | `instrument` is a command noun owned by one extension, which owns the noun and a registry of instrument kinds and nothing else: no capture, frames, triggers, journal or connector kind. |
| INS-002 | A kind is registered by the manifest block `[[instrument_kinds]]` or the entry-point group `axiom.instrument_kinds`, and passes the portfolio gate before it is imported. Axiom's own kinds register the same way. |
| INS-003 | A kind name declared twice is used by neither, and `instrument list --kinds` names both origins. A kind the gate refused, or one that cannot load, is listed with the reason and the fix, and every other kind keeps working. |
| INS-004 | `<cli> instrument <kind> <verb> ...` reaches the kind's own verbs with the rest of the command line; the output and exit code are the kind's. |
| INS-005 | An unknown kind is refused (exit 2) with the registered kinds, the nearest match, and the command the person typed, under any product's brand. |
| INS-006 | `instrument list` lists every instrument every kind reports (kind, name, health, state); `instrument list --kinds` lists the registered kinds (name, what each yields, origin, description). |
| INS-007 | `instrument status` gives one summary per kind; a kind that fails to answer is shown as failed with its error, and the other kinds still answer. |
| INS-008 | Each of the noun's own verbs is a skill (`instrument.list`, `instrument.status`), and each is a read. A kind's verbs keep their own extension's skill names (`video.add`). |
| INS-009 | Help, Tab completion and the `next:` lines reach a kind's verbs and flags (`instrument video add --url`), and know each verb's skill and risk. |
| INS-010 | The noun belongs to node function `acquire` (ADR-164). |
| INS-011 | The kind contract assumes no frames, no bus, no triggers, no source kind and no connector file. |
| INS-012 | Triggers and events extend to every kind (ADR-256 D7): an event has one kind-neutral shape (instrument, trigger and version, time, `started` / `ended` / `occurred`, incident key, severity, score, detail; never a recipient), every kind's events go to one ledger per node, and a kind may offer triggers over what it yields. Video's are the first; nothing a consumer's video trigger emits changes when they move under `instrument`. |

Video is the first kind (ADR-240, [prd-video](prd-video.md)): its verbs are
`<cli> instrument video <verb>`.

Out of scope: any kind but video; a scalar kind; a shared capture lifecycle (claims, boot
unit, diagnose ladder); moving the event ledger and `events` out of video before a second
kind needs them (INS-012 constrains their shape now); any change outside Axiom, including a consumer
layer's data-acquisition extension.

## 6) Constraints

- `instrument --help` and `instrument list --kinds` import no kind's heavy dependencies
  (no `numpy`, no `ffmpeg`).
- A broken or refused kind never takes the noun down.
- No output names a credential.
- The uncertainty posture is `not-applicable`: the noun measures nothing, and each kind
  declares its own.
- Every verb is a skill function (ADR-056); `axi ext lint` passes.

## 7) Phases

- Phase 1: the noun, the registry, `list` and `status`, with video as the first kind
  (INS-001..011). It lands before or with video phase 1.
- Later: a scalar kind, when its name and shape are decided (ADR-256, open questions),
  with the shared event ledger and `events` read it needs (INS-012).

## 8) Risks and open questions

- "Kind" has three meanings on one command line (instrument kind `video`, video source
  kind `rtsp`, connector kind `video`). Help text names each in full.
- Per-kind gates do not exist: every kind runs under `acquire` and the noun's
  availability gate.
- For the code owner: the scalar kind's name and shape, and whether it is the control
  surface of `data_platform/daq`; when the shared lifecycle is extracted.

## 9) Acceptance

- The unit tests in spec-instrument §Tests pass, including a scalar-shaped fake kind
  registered by entry point.
- By hand, through `axi` and through a branded CLI: `instrument list --kinds` shows
  `video`; `instrument video add / verify / start / status / events / stop` runs against
  the simulated camera; every fix names the command the person typed.
