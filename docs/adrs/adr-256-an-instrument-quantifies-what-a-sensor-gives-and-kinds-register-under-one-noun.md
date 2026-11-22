# ADR-256: An instrument quantifies what a sensor gives; `instrument` is the command noun, and kinds register under it

**Status:** Proposed (2026-11-11)
**Related:** [ADR-056](adr-056-skill-as-function.md) (skills as functions; departed from in D5),
[ADR-243](adr-243-people-see-a-log-code-keeps-logbook-and-log-is-the-cli-noun.md) (one noun, a registered word in the second position, skill names unchanged: the pattern reused here),
[ADR-240](adr-240-a-video-source-is-a-connector-with-a-kind-and-a-trigger-is-a-registered-contract.md) (video, the first kind),
ADR-023-A1 (portfolio gate on discovered code), ADR-110 §1 (one registry mechanism),
[ADR-164](adr-164-a-node-declares-its-functions.md) (node functions),
[ADR-047](adr-047-availability-aware-cli-dispatcher.md) (availability gate),
[ADR-106](adr-106-push-ingest-gateway.md) and [spec-signal-ingest-and-producer](../specs/spec-signal-ingest-and-producer.md) (the scalar data path),
[prd-instrument](../prds/prd-instrument.md), [spec-instrument](../specs/spec-instrument.md)

## Context

A node takes things in from the physical world: frames from a camera today, readings
from a probe or a meter later. Each of those would otherwise arrive with a command noun
of its own (`axi video`, then the next one), and an operator would have to know which
noun holds which thing. The code owner asked for one noun, `instrument`, for all of it,
with video as the first.

Nothing has shipped under `axi video`; the video extension is on branches. A noun that
ships has to be renamed with a deprecation period (ADR-243 D6), so the decision is cheap
now and expensive after the first release.

Four facts about the platform shape the decision:

- **A noun has one owner, and the first one found wins.** Discovery keeps the first
  extension that provides a noun and skips the rest, and installed packages are searched
  before Axiom's builtins (`extensions/discovery.py`). A consumer package that provided
  `instrument` would hide Axiom's, every kind with it, with no error.
  `tests/extensions/test_cli_noun_collisions.py` only sees Axiom's own builtins. ADR-243 D5
  met the same problem for `log` and decided that a product does not override the noun.
- **Skill tooling reads the namespace as the text before the first dot.**
  `skills_emit._short_name` and `skill_dispatch.capability_namespace` both split there, so
  a three-part name such as `instrument.video.add` has the leaf `video.add` and the
  namespace `instrument` for every kind. ADR-056 says the namespace is the providing
  extension's noun, but six manifest-declared skill namespaces already name no noun, and
  ADR-243 D1 keeps `attest.*` under the noun `log`.
- **The command-path-to-skill join assumes two words.** `cli/command_space.py` turns
  `data show` into `data.show`, which is how Tab completion, `explore` and the `next:`
  lines learn a verb's skill and risk. It cannot work when the second word is a
  registered name; ADR-243 proposed an exported map for that case.
- **The data paths differ by what is produced.** Frames go over a lossy, in-process bus
  to triggers (ADR-240). Scalar readings are journaled and sent to an ingest face, and
  must not be lost (`data_platform/daq`, ADR-106). One abstraction over both would be
  wrong for both.

## Decision

**`instrument` is the command noun for everything a node quantifies from its sensors.
One thin extension owns the noun and a registry of instrument kinds; each kind
registers through the gate and never claims the noun.**

### D1: The words

- A **sensor** is the physical device: a camera, a temperature probe, a power meter. It
  produces a raw output and knows nothing about the node.
- An **instrument** is the part on the node that turns a sensor's output into
  quantified data: a video frame, a reading with a unit. One declared instance is one
  instrument (the video source `cam-1` is an instrument of kind `video`; the camera
  behind it is its sensor).
- An **instrument kind** is defined by what it quantifies into, not by the physical
  quantity. Video yields frames. A future scalar kind would yield readings with a unit,
  and temperature and power would be channels of that one kind, told apart by declared
  data rather than by code. The scalar kind's name and shape are not decided here.

### D2: One thin owner

The `instrument` extension owns the noun, the kind registry, and two verbs of its own:
`instrument list` (every instrument every kind reports, or with `--kinds` the registered
kinds) and `instrument status` (one summary per kind). It owns no capture, frames,
decoding, triggers, journal or connector kind. The noun carries node function `acquire`
(ADR-164) and the availability gate (ADR-047) for every kind under it.

### D3: Kinds register; they never claim the noun

A kind is declared by the manifest block `[[instrument_kinds]]` or the entry-point group
`axiom.instrument_kinds`. Both pass the portfolio gate (ADR-023-A1) before anything is
imported, as video triggers do (ADR-240 D3). Axiom's own kinds register the same way, so
a consumer's kind comes in through the same door as video. A kind name declared twice is
used by neither, and both origins are named. A product does not provide `instrument` as a
noun; it ships a kind.

### D4: The kind owns its verbs

`<cli> instrument <kind> <verb> ...` hands the rest of the command line to the kind's own
command entry, with the program name the person typed. The kind owns its verbs, flags,
output and exit code. `video` drops its `cmd` provider and registers kind `video`, so
`axi video add` becomes `axi instrument video add`.

### D5: Skill names follow the kind's extension

Video's skills stay `video.add`, `video.status` and so on, and their MCP tool names do
not change. This departs from ADR-056's "the registry namespaces skills under their
extension's noun" in the same way ADR-243 D1 does. The join from command path to skill
comes from a map the noun's module exports (`SKILL_FOR_VERB`, as ADR-243 proposed),
built from what each kind declares, and `command_space` reads it before it joins words.

### D6: Share the control surface, not the data path or the lifecycle

The kind contract assumes no frames, no bus, no triggers, no source kind and no
connector file. Claims, the boot unit and the diagnose ladder stay in `video` until a
second real kind exists; they are extracted from two examples, not one. `instrument`
measures nothing, so its uncertainty posture is `not-applicable`; each kind declares its
own.

### D7: Triggers and events belong to every instrument, not to video

A trigger watches what an instrument yields and emits events; that holds for every kind,
not only video. The event is one kind-neutral shape (the instrument it came from, the
trigger and its version, time, `started` / `ended` / `occurred`, a stable incident key,
a severity from the trigger's own configuration, a score and detail; never a recipient,
ADR-238 D2), and every kind's events go to one event ledger per node, read through one
`events` surface. What a trigger observes is the kind's own output (frames for video,
readings for a scalar kind), so the observing half of the contract is per kind, over the
shared event shape, and D6 still holds: the data paths stay separate. Trigger discovery
is gated the same way for every kind (ADR-023-A1). Video's `StreamTrigger`,
`TriggerEvent` and ledger (ADR-240 D3) are the first case; their event shape and ledger
are written so they can move under `instrument` without changing what a consumer's
trigger emits. The kind contract in D6 stays free of triggers: a kind offers them, the
noun does not require them.

## Options considered

- **Keep `axi video` and add `instrument` later.** The rename would then need forwarders
  and a deprecation period for every script and fix that names `axi video`. Rejected:
  nothing has shipped, so moving now costs edits only.
- **Fold video into the instrument extension (`instrument/video/`).** One extension, one
  manifest. Rejected: `ffmpeg`, the `video` extra and the capture service would sit on the
  umbrella, and Axiom's kind would reach the noun by a door a consumer's kind cannot use.
- **Nested nouns in the platform: a `cmd` provider declaring `noun = "instrument video"`.**
  The dispatcher, help, completion and both gates would then work per kind. Rejected for
  now: every noun-keyed surface changes (dispatch, help engine, completion index,
  vocabulary, collision guard, node functions) for one noun, while a registry under one
  noun is the shape ADR-243 already chose for `log`. If a third noun needs nesting, this
  is worth revisiting.
- **Three-part skill names (`instrument.video.add`).** Rejected: the tooling above reads
  `instrument` as every kind's namespace and `video.add` as the leaf, the MCP tool names
  change, and the command-path join still needs a map.
- **One data abstraction over frames and readings.** Rejected: frames are lossy and
  in-process, readings are journaled and must not be lost. Each kind keeps its own path.
- **Extract a shared lifecycle now.** Rejected: one example cannot show which parts are
  shared. The contract stays minimal until the second kind.
- **Triggers and events stay video's.** Simpler today. Rejected by the code owner: "did
  anything happen" is the same question for a reading crossing a threshold as for motion
  in a frame, and a second ledger and `events` verb per kind would split one record of
  what a node saw.

## Consequences

- Every fix string, skill precondition, document and test that names `axi video` moves to
  `axi instrument video` before the video extension merges (ADR-240, prd-video,
  spec-video).
- Platform work follows from D5 and from the second position being a registered name:
  `command_space` reads an exported verb-to-skill map (shared with ADR-243), the
  completion tree's fingerprint covers each kind's command module and the installed kinds,
  and chat's `/skills` shows a skill's command path from the annotated tree.
  spec-instrument lists each as an acceptance row.
- Per-kind gates do not exist: every kind runs under `acquire` and the noun's availability.
  A kind's own requirements (for video, `ffmpeg` and `numpy`) stay preconditions of its
  skills.
- The word "kind" now has three meanings on one command line: instrument kind (`video`),
  video source kind (`rtsp`) and connector kind (`video`). Help text names each in full.
- Existing documents use "instrument" for the physical emitter
  (prd-signal-ingest-and-producer, spec-signal-ingest-and-producer). Under D1 that is a
  sensor. Those documents are amended when the scalar kind is designed.
- A consumer layer's data-acquisition extension is expected to become a scalar kind under
  this noun later. Not now: this decision touches no repository but Axiom.
- Under D7, video's event shape and ledger must not assume a frame where they do not
  need one; a consumer's trigger written against ADR-240 keeps working when they move.
  Where the shared event type, ledger and `events` verb live, and the per-kind observing
  contract for readings, are designed with the second kind.
- Open, for the code owner: the scalar kind's name and shape, and whether it is the
  control surface of `data_platform/daq`; when the shared lifecycle is extracted.
