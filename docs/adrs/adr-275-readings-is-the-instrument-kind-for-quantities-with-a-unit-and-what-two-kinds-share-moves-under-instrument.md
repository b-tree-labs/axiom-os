# ADR-275: `readings` is the instrument kind for quantities with a unit, and what two kinds share moves under `instrument`

**Status:** Proposed (2026-11-22)
**Related:** [ADR-256](adr-256-an-instrument-quantifies-what-a-sensor-gives-and-kinds-register-under-one-noun.md) (the noun and its kinds; this answers its open questions),
[ADR-240](adr-240-a-video-source-is-a-connector-with-a-kind-and-a-trigger-is-a-registered-contract.md) (video, the first kind),
[ADR-264](adr-264-a-video-trigger-can-take-the-events-of-the-triggers-before-it-on-its-source.md) (a trigger takes another's events),
[ADR-267](adr-267-a-video-trigger-states-the-sensitivity-of-what-it-writes.md) (sensitivity),
[ADR-265](adr-265-everything-the-system-learns-is-an-observation-sensing-and-signals-are-two-families-of-one-path.md) (a reading is an observation),
[ADR-206](adr-206-silver-identity-is-the-reading-and-a-rebuild-replaces-in-place.md) (a reading's identity),
ADR-182 (no downtime from our own causes), ADR-023-A1 (portfolio gate),
[prd-readings](../prds/prd-readings.md), [spec-readings](../specs/spec-readings.md), [spec-instrument](../specs/spec-instrument.md)

## Context

A node's second kind of sensor is one that sends numbers: a control console that
streams its power, temperatures and positions several times a second, and later a
data-acquisition chassis that samples thermocouples. ADR-256 made `instrument` the noun
and video its first kind, and left four questions to the second kind:

- the scalar kind's name and shape, and whether it is the control surface of
  `data_platform/daq`;
- where the shared event shape, ledger and `events` read live;
- what a trigger on readings observes;
- when the lifecycle the kinds share (claims, the boot unit, the diagnose ladder) is
  extracted. ADR-256 refused to extract it from one example.

Three facts shape the answers:

- **Readings must not be lost, and the DAQ core already keeps them.** `data_platform/daq`
  has the journal written first, its per-consumer cursors, overflow rules by
  sensitivity, and the hash-chained envelope. What it lacks is what video has: a
  declared connector, a claim, a capture process that survives a reboot, `verify`,
  `status`, `diagnose` and triggers.
- **A reading's identity is `(site, feed, channel, ts, source_class)`** (ADR-206), and
  ADR-265 says `ts` is when the value was true (`observed_at`), apart from when the node
  learned it (`recorded_at`). The DAQ record's `ts` is the reader's wall clock today, an
  open question in its spec.
- **Video's switch hands a source over by claim**, and a frame lost in the handover is
  acceptable. A reading lost in it is not: a collector updates by running old and new side
  by side and dropping repeats by identity (ADR-182).

## Decision

**`readings` is the instrument kind whose instruments yield readings: quantities with a
unit, one per channel, at instants the sensor timed. It follows video's shape: a
connector of its own kind, a source-kind registry, a capture service holding each
instrument by claim, triggers named in the connector file, and events in the shared
shape. What the two kinds share moves under `instrument`; each kind keeps its data
path.**

### D1: The kind

The kind is `readings` (ADR-265 D2: a reading is a quantity with a unit), in its own
extension. Its verbs are `<cli> instrument readings <verb>` and its skills are
`readings.<verb>` (ADR-256 D4, D5). It has the verbs video has: `add`, `verify`,
`list`, `status`, `start`, `stop`, `events`, `diagnose`, `install`, `uninstall`,
`revoke`. It has no verb that prints readings; what reads them is a trigger.

### D2: An instrument is a connector of kind `readings`, and its channels are declared

One file per instrument, `<state>/plinth/connectors/<name>.toml`, as for video. The
file names a **source kind** and its settings, which say where each channel sits in
what the sensor sends, and a **channel map**, which says what each channel is. The
channel map is the site's file in the shape conformance already reads (`schema_ref`
and `[channel."<name>"]` with its `unit`), so capture and conformance read one
declaration. `add` and `verify` refuse a channel the source yields that the map does not
declare, and one the map declares that the source does not yield. Temperature, power
and position are channels of one instrument, told apart by that data alone (ADR-256 D1).

### D3: Source kinds register, and the first reads fixed binary records over TCP

A registry like video's (ADR-110 §1) turns `source.kind` into a source. A source opens
sessions that yield instants: the sensor's own time and every channel's value. Axiom's
first kind is `tcp_struct`: fixed-size records over a TCP connection, decoded by a
`struct` format, one field naming the record's time. A consumer registers its own kinds
the same way. A data-acquisition chassis is a later kind.

### D4: Capture journals every instant first, timed by the sensor

The capture service holds each started instrument by claim (video's mechanism). The
holder journals every instant in the instrument's DAQ journal,
`<state>/readings/journal/<name>/`, before anything else sees it. This kind is the
control surface of the DAQ core: the journal, the consolidator and the envelope are
`data_platform/daq`'s, used as they are.

A record's `ts` is the sensor's time. The node's receive time is kept beside it
(`tags.received_at`), and the difference is shown by `verify` and `status`, never
corrected. A source kind that cannot time its records is not a readings source kind.

### D5: A switch overlaps, and repeats are dropped by identity

A copy that finds the instrument held by another live copy reads it anyway and keeps
what it reads in memory. When it takes the claim, it journals what it kept, less every
record the journal already holds with that time, then carries on. Only the claim holder
writes the journal, so the journal has one writer. An update is two copies side by side,
and no reading is lost or written twice, provided the sensor serves two readers at once.

### D6: Triggers read the journal

A readings trigger observes `Record`s: one instant of one instrument, with its channel
values, its time, its receive time and its `seq`. The holder runs the instrument's
triggers on one thread, in the order their `on` lists need, reading the journal through
a cursor of their own. A trigger never slows capture, a restart resumes where the
triggers left off, and a record is never dropped for a slow trigger. Triggers are
discovered through `[[readings_triggers]]` and `axiom.readings_triggers` behind the
portfolio gate, are attached in the connector file, take other triggers' events with
`on` (ADR-264), and state their sensitivity (ADR-267). No trigger ships with the kind.

### D7: Events have one shape, and each kind keeps them where video does

Every kind's triggers emit the same `TriggerEvent`. The event's `stream` is the
instrument's name. Each instrument's events go to a DAQ journal of their own,
`<state>/readings/events/<name>/`, written the way video writes its own. They are read by
`instrument readings events`, as video's are read by `instrument video events`. One
`instrument events` read across every kind is not built.

### D8: What two kinds share moves under `instrument`

These move from video to `instrument/shared/`, a library a kind uses. They are not part
of the `InstrumentKind` contract, and the library holds code, not data: `instrument`
still runs no capture and keeps no journal or trigger of its own (ADR-256 D2). Each kind
runs them, under its own directory.

- the event shape and its checks;
- trigger attachment and gated discovery, each kind naming its own manifest block and
  entry-point group;
- the event journal;
- the state a kind keeps for an instrument: claims, started marks, the last verify,
  live snapshots, reload requests, open episodes and logs, each under
  `<state>/<kind>/`;
- starting and stopping a capture process;
- the boot unit;
- the diagnose ladder.

Each kind binds them to its own directory and names. Video's modules keep every name
they export, its files stay where they are, and nothing it writes changes. The
supervisor, the trigger runner, the source registry and the connector stay per kind,
because they are the data path (ADR-256 D6). The reconnect back-off moves too: it is
not the data path, and both supervisors use it.

## Options considered

- **A scalar kind per quantity (`temperature`, `power`).** Rejected by ADR-256 D1: the
  physical quantity is declared data, not a kind.
- **A consumer layer's DAQ providers as the kind (its own `daq run` verb).** That path has no claim,
  no boot unit, no `verify` and no triggers, and it is a consumer layer's. The kind uses
  the same DAQ core under the noun every instrument has.
- **The node's receive time as `ts`.** Two copies would stamp one reading twice, so an
  overlap update could not tell a repeat from a reading. That conflicts with ADR-206's
  identity and ADR-265's `observed_at`.
- **Video's switch (the successor waits for the claim).** It loses every reading the
  sensor sends during the handover.
- **Two writers on one journal during a switch.** Every append would have to re-read
  what the other wrote, and the journal is large. One writer, and the successor's
  buffer, is simpler.
- **One thread per trigger, each fed from a bounded queue (video's runner).** A slow
  trigger would lose readings. A cursor on the journal loses none.
- **Copy video's lifecycle into readings.** The claims, the boot unit, the event journal
  and the portfolio gate would each exist twice. ADR-256 kept them in video until a
  second kind could show what they share. Readings is that kind.
- **Move video's events into one node-wide ledger now.** That moves data on running
  nodes for no reader that needs it yet.

## Consequences

- A sensor that sends numbers becomes an instrument by declaration: a connector file,
  a channel map, `verify`, `start`.
- The site's channel map is the one declaration capture and conformance read, so a
  channel a deposit names but the map lacks is refused at `add`, not found at
  conformance.
- A consumer's readings trigger is a package, as a video trigger is. The first consumer
  of readings is a domain product's trigger.
- Video's internals move. Every name a consumer imports from video stays, and video's
  tests run unchanged.
- The DAQ record's `ts` question is answered for readings: the sensor's time.
- Not done here: sending the journal to an ingest point (a transmitter cursor on the
  same journal); a node-wide `instrument events`; more source kinds; a data-acquisition
  chassis, which reads blocks of hardware-timed samples and may need a block record.
