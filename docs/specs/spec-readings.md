# Spec: Readings

**Owner:** B-Tree Labs • **Status:** Draft • **Last updated:** 2026-11-22
**PRD:** [prd-readings.md](../prds/prd-readings.md) • **Key ADRs:** [ADR-275](../adrs/adr-275-readings-is-the-instrument-kind-for-quantities-with-a-unit-and-what-two-kinds-share-moves-under-instrument.md), [ADR-256](../adrs/adr-256-an-instrument-quantifies-what-a-sensor-gives-and-kinds-register-under-one-noun.md), [ADR-240](../adrs/adr-240-a-video-source-is-a-connector-with-a-kind-and-a-trigger-is-a-registered-contract.md), [ADR-264](../adrs/adr-264-a-video-trigger-can-take-the-events-of-the-triggers-before-it-on-its-source.md), [ADR-267](../adrs/adr-267-a-video-trigger-states-the-sensitivity-of-what-it-writes.md)

## Overview

The `readings` extension (`src/axiom/extensions/builtins/readings/`) is instrument kind
`readings`: instruments that yield quantities with a unit. An instrument is a connector
of kind `readings`. Its source kind says how to read the sensor and where each channel
sits in what it sends; its channel map, a site file, says what each channel is. A
capture service holds each started instrument once, journals every record in the
instrument's DAQ journal, and runs the instrument's triggers on what it journaled.

| Layer | Owns |
|---|---|
| `readings` | the connector kind, the source-kind registry and `tcp_struct`, the supervisor, the journal writer, the trigger runner, `Record`, the verbs and skills |
| `instrument` | the noun; the shared event shape, trigger discovery, event journal, state files, capture process control, boot unit and diagnose ladder (§What the kinds share) |
| `data_platform/daq` | the journal, the consolidator and the envelope, used unchanged |
| the site | the connector file's values and the channel map |

Words, as ADR-256 D1 and ADR-265 use them: the **sensor** is the device; the
**instrument** is the declared connector and its capture; a **channel** is one named
quantity; a **reading** is one channel's value at one instant; a **record** is one
journaled instant with every channel's reading.

## Contracts

### Connector kind `readings`

```toml
# <state>/plinth/connectors/console-1.toml
[connector]
name = "console-1"
kind = "readings"
# credential_ref = "foreign://readings-console-1"   # only for a source that needs a login

[connector.params]
channel_map = "/srv/site/channel-maps/console-v1.toml"   # what each channel is
sensitivity = "internal"                                 # the class of every record
max_journal_mb = 1024                                    # the journal's size bound

[connector.params.source]                                # what the source kind reads
kind = "tcp_struct"
host = "console.example.org"
port = 5556
format = "<d11f"
fields = ["ts", "A", "B", "C", "D", "power", "temp1", "temp2", "temp3", "n1", "n2", "n3"]
time = "ts"

# [[connector.params.triggers]]                          # zero or more (§Triggers)
# name = "<trigger>"
```

| Key | Rule |
|---|---|
| `name` | `^[a-z0-9][a-z0-9_-]{0,63}$`; unique among every connector on the node |
| `channel_map` | an absolute path to a channel map (§Channel map); required |
| `sensitivity` | a DAQ sensitivity class (`open`, `internal`, `ec-controlled`, `itar`, `restricted`); `internal` by default |
| `max_journal_mb` | a whole number from 1 to 1048576; `1024` by default |
| `source.kind` | a registered source kind; the other keys are that kind's |
| `credential_ref` | a secret reference, resolved when a session opens; never written anywhere else |

`validate` refuses a declaration that could never work, with the fix, before anything
is contacted.

### Channel map

The site's file, in the shape conformance reads:

```toml
schema_ref = "site-a/console-v1"

[channel."power"]
unit = "W"
role = "power"            # read by others; readings carries it through
description = "..."
```

Readings reads `schema_ref`, which becomes every record's `schema_id`, and each
`[channel."<name>"]` table's `unit`, which every channel must have. Everything else
in the file belongs to its other readers. A map that does not parse, has no
`schema_ref`, declares no channel, or has a channel without a unit is refused with the
file and the fix.

**Coverage** (RDG-003). The channels the source yields, which a source kind knows from
its settings alone, must be exactly the map's channels. `add` and `verify` name every
channel the map lacks ("declare it as `[channel."X"]` in <map>") and every channel the
source does not yield ("remove it from <map>, or add it to the source's fields").

### `ReadingsSource` and the source-kind registry

```python
@dataclass(frozen=True)
class Instant:
    ts: float                                   # the sensor's time: seconds since the epoch, UTC
    values: Mapping[str, float | int | None]    # every channel's value; None when not a finite number

class ReadingsSession(Protocol):
    partial: int                                             # records cut short, dropped
    def read(self, timeout_s: float) -> Instant | None: ...  # None: the sensor ended the session
    def close(self) -> None: ...                             # from any thread; a blocked read returns

@runtime_checkable
class ReadingsSource(Protocol):
    kind: str
    channels: tuple[str, ...]                  # what it yields, from its settings alone
    def detect(self) -> DetectResult: ...      # details["steps"]: the rungs diagnose climbs
    def open(self) -> ReadingsSession: ...     # raises ReadingsSourceError with a fix
    def redacted(self) -> dict[str, Any]: ...  # what status may print: never a credential
```

`read` raises `StallTimeout` when no instant arrives in `timeout_s`, and
`ReadingsSourceError` when what arrived cannot be read. A `SourceKind` registers
`kind`, `factory(params, credential) -> ReadingsSource`, its `SourceParam`s (the flags
`add` offers; a `multiple` parameter is a comma-separated flag and a list in the file,
and an `int` or `float` one is written as a number) and a description, on `ConnectorRegistry` (ADR-110 §1): a duplicate is loud,
re-registering the same entry is harmless, an unknown kind names the known ones.
Validating builds the source without a credential and contacts nothing.

`ReadingsSourceError(message, code, fix)` codes: `invalid-config`, `unreachable`,
`timeout`, `no-record`, `bad-record`, `channel-map`, `channels`, `credential-unresolved`,
`not-declared`, `error`.

### Source kind `tcp_struct`

Fixed-size binary records over one TCP connection.

| Param | Rule |
|---|---|
| `host`, `port` | required; the sensor's address |
| `format` | required; a `struct` format that starts with a byte order (`<`, `>`, `!` or `=`) and holds only numbers and pad bytes (`x`) |
| `fields` | required; one unique name per value the format yields, in order |
| `time` | required; the field holding the record's time in seconds since the epoch. It is not a channel |
| `connect_timeout_s` | `10` by default |

The channels are `fields` without `time`. A session reads exactly `struct.calcsize(format)`
bytes per record. A connection closed in the middle of a record ends the session, and
the part is dropped and counted. A float that is not finite is `None`. Detect rungs:
`host-resolves` → `port-accepts` → `record-arrives` (one whole record within
`connect_timeout_s`) → `time-is-epoch-seconds` (the time field reads as a date in 2000 to
2100). The kind reads; it never writes to the sensor.

### `Record`: what a trigger observes

```python
@dataclass(frozen=True)
class Record:
    stream: str                               # the instrument's name
    ts: str                                   # the sensor's time, ISO-8601 UTC
    received_at: str                          # the node's time, ISO-8601 UTC
    values: Mapping[str, float | int | None]  # by channel
    units: Mapping[str, str]                  # by channel, from the channel map
    seq: int                                  # the record's seq in the instrument's feed
```

### The journal

Every instant is one record in `<state>/readings/journal/<name>/`, a `DAQJournal`
written through a `DAQConsolidator` (spec-signal-ingest-and-producer).

| Field | Value |
|---|---|
| envelope `producer_id`, `feed` | `readings-<name>`, `readings.<name>` |
| envelope `source_class`, `model_ref` | `measured`, none |
| envelope `delivery_class`, `payload_kind` | `standard`, `records` |
| record `schema_id` | the channel map's `schema_ref` |
| record `ts` | the instant's time (RDG-006) |
| record `values` | every channel's value; `null` for one that was not a finite number |
| record `tags` | `received_at`: when the node read it |
| record `quality` | `good`, or `uncertain` when a value is `null` |
| record `sensitivity` | the connector's `sensitivity` |

**One writer** (RDG-008). Only the copy holding the instrument's claim writes its journal.

**Repeats** (RDG-008). A record's identity in its feed is its `ts`. The writer keeps the
times of the newest 10000 records, as many as a waiting copy keeps, read from the
journal's tail when it opens it; a record whose time is among them is not written, and is
counted in `repeats`.

**Size** (RDG-009). `max_journal_mb` bounds the journal. For `open` and `internal` the
oldest segments are dropped (`drop_oldest`, counted in `dropped`); for a controlled
class the writer refuses (`block_producer`), and every record it could not write is
counted in `unwritten` and makes the instrument `degraded`, with the fix. Nothing
compacts the journal: no transmitter reads it yet.

### Triggers

A readings trigger is the contract video's is (spec-video §`StreamTrigger` and
`TriggerEvent`), observing `Record`s:

```python
@runtime_checkable
class ReadingsTrigger(Protocol):
    name: str
    version: str
    def start(self) -> None: ...
    def observe(self, record: Record) -> Iterable[TriggerEvent]: ...
    def health(self) -> dict[str, Any]: ...
    def stop(self) -> None: ...
```

`TakesEvents.on_event(event)` (ADR-264), `BaseTrigger` (with `event(record, kind,
episode_id, ...)`), `sensitivity` (ADR-267) and attachment by
`[[connector.params.triggers]]` with `name`, `severity` and `on` are as for video.
Discovery: the manifest block `[[readings_triggers]]` and the entry-point group
`axiom.readings_triggers`, both behind the portfolio gate before import; the AEOS
manifest schema has a root-level `readings_triggers` array (`name`, `factory`) beside
`video_triggers`. A name declared twice is used by neither. No trigger ships with the
extension.

**The runner** (RDG-012). The holder runs the instrument's triggers on one thread. It
reads the journal through the cursor `triggers`, which starts at the journal's end the
first time and is committed after each record, so a restart resumes at the next one.
The supervisor wakes it after each write; nothing polls. For each record, each trigger in
turn, upstreams before the triggers on them and otherwise in attachment order, is given
the events written since its last record by the attachments it is on, then the record.
Every event is checked as video's runner checks it (its stream, its trigger, an episode
started once and ended once) and journaled with its trigger's declared sensitivity. A
trigger that raises is counted and fed the next record; one that raises
`TriggerConfigError` is `failed` with its fix and fed nothing more. Open episodes are
kept on disk and closed by the capture service, with the reason, when capture stops,
when a reload replaces or detaches the trigger, when a new holder finds them left open,
and on `revoke`. `connector reload <name>` applies a changed attachment list atomically,
as for video.

### Events

Each instrument's events are journaled in `<state>/readings/events/<name>/`, written
the way video's are (spec-video §Events), with these values:

| Field | Value |
|---|---|
| envelope `producer_id`, `feed` | `readings-<name>`, `readings.<trigger name>` |
| envelope `source_class`, `model_ref` | `estimated`, the trigger's `name@version` |
| record `schema_id` | `readings/trigger-event-v1` |
| record `values`, `tags` | `kind`, `score`, `detail`; `stream`, `trigger`, `incident_key`, `severity` |

`events [<name>] [--since <iso>]` lists them oldest first, as `instrument video events`
does. `revoke` keeps both journals.

### CLI `<cli> instrument readings` — each verb is skill `readings.<verb>`

| Verb | Params | Output |
|---|---|---|
| `add <name> --kind tcp_struct --host --port --format --fields a,b,c --time <field> --channel-map <file> [--sensitivity] [--max-journal-mb] [--credential-ref]` | writes the connector, runs `verify` | the verify report |
| `verify <name>` | — | records read, rate, channels with their units, the sensor's clock against the node's (median `received_at − ts`), the first record's latency |
| `list [--kinds \| --triggers]` | — | instruments; source kinds; triggers, with any refused or declared twice |
| `status [<name>]` | — | per instrument: source kind, state, pid, session, records, rate, last record's age, reconnects, partial records, repeats, clock offset, journal (records, bytes, dropped, unwritten), triggers and their health |
| `start <name>` / `stop <name>` | — | marks it started or not, and brings its capture up or down |
| `events [<name>] [--since]` | — | its events (§Events) |
| `diagnose <name>` | — | declared → source kind registered → channel map reads → credential resolves → the source kind's detect rungs → the channels match the map → verify; the first failure names its fix |
| `install` / `uninstall` | — | the boot unit `readings-capture` |
| `revoke <name>` | — | stops capture, closes open episodes, deletes the declaration and its records; keeps both journals |

Every verb takes `--json`. The reads (`list`, `status`, `diagnose`, `events`) are MCP
tools. Fixes name `<cli> instrument readings` (`naming.command()`); the capture process
gets the CLI's name in `AXIOM_READINGS_CLI`.

**The instrument kind.** `readings/instrument.py` defines `KIND` (`name = "readings"`,
`yields = "readings"`), declared as `[[instrument_kinds]]`. `list()` gives one row per
instrument with its health, state and source kind; `status()` one summary line.

## Design

**Started and held.** As for video: `start` sets `<state>/readings/started/<name>` and
spawns a capture process unless a live copy holds the claim, or hands the instrument to
a running `--all` service. `stop` clears the mark, and the holder lets go within the
drain time.

**The supervisor** (`capture.py`). One per started instrument, on a thread of the
capture service:

1. **Waiting.** Another live copy holds the claim. The supervisor opens a session anyway
   and keeps every instant it reads, the newest 10000, in memory, counting what it drops
   past that. It takes the claim when the holder releases it or goes stale.
2. **Holding.** It opens the journal, writes what it kept less repeats, and reads the
   session. Each instant is written, then the runner is woken. The claim is touched as
   records arrive and through bounded waits.
3. **Reconnecting.** A session that ends, stalls (no record for 10 s; the first gets
   10 s more) or fails is closed, and a new one opens after a back-off from 1 s to 60 s
   with jitter.
4. **Stopping.** On stop or `SIGTERM` the supervisor closes the session, stops the
   triggers (closing their episodes), writes its last snapshot and releases the claim,
   in that order.

An update starts the new copy beside the old one. The new copy waits and reads; the old
copy stops; the new one takes over and writes what it read, less what the old one
already wrote. The test is a live feed through a switch with every record journaled
exactly once and `seq` gapless.

**Live snapshot.** The holder writes `<state>/readings/live/<name>.json` about once a
second: state, pid, copy, session, counters, the clock offset (median of the newest 50
`received_at − ts`), the journal's health, the triggers' health and the last reload's
answer. A waiting copy writes `standby/<name>.<copy>.json`. `status` reads these from
another process.

**Files.** Under `<state>/readings/`: `claims/`, `started/`, `verify/`, `live/`,
`standby/`, `logs/`, `reload/`, `episodes/`, `services/`, `journal/<name>/` and
`events/<name>/`. No credential is written anywhere.

**Modules.**

| Module | Holds |
|---|---|
| `connector.py` | `ReadingsConnector`, validate, save, load, list, revoke, verify, health |
| `channels.py` | reading a channel map, and coverage |
| `sources/__init__.py`, `sources/tcp_struct.py` | the registry and the first kind |
| `record.py` | `Record`, and a journaled record to a `Record` |
| `journal.py` | the writer: repeats, size, overflow |
| `capture.py`, `services/capture.py` | the supervisor and the capture service |
| `runner.py`, `triggers.py` | the trigger runner, `ReadingsTrigger`, `BaseTrigger`, the catalogue |
| `status.py`, `diagnose.py`, `reload.py`, `connector_status.py` | the reads, and the connector family's hooks |
| `instrument.py`, `naming.py`, `commands/cli.py`, `skills/` | the kind, the command, the verbs |

### What the kinds share

The event shape, trigger attachment and discovery, the event journal, the state files,
starting and stopping a capture process, the boot unit, the diagnose ladder and the
back-off are `instrument/shared/`'s ([spec-instrument](spec-instrument.md) §What the
kinds share), bound to `readings`: `<state>/readings/`, `[[readings_triggers]]`,
`axiom.readings_triggers`, `readings-capture` and `AXIOM_READINGS_CLI`.

## Tests

Unit, in `src/axiom/extensions/builtins/readings/tests/`, written first, against a fake
sensor: a TCP server on a free port that sends `struct`-packed records with increasing
times, can stop and resume, and serves several readers at once.

- A connector file of kind `readings` round-trips; validate refuses a bad name, a
  missing or unreadable channel map, an unknown source kind, a bad sensitivity and a bad
  size, each with its fix (RDG-001).
- `tcp_struct` refuses a format without a byte order, one with text, fields that do not
  match the format, and a time that is not a field; it reads records, drops a partial
  one, and gives `None` for a non-finite float (RDG-002).
- Coverage: a missing channel and an extra one are each named, at `add` and at
  `verify` (RDG-003).
- Verify reports records, rate, channels and units, and the clock offset; a failed one
  leaves the instrument inactive (RDG-004).
- A started instrument with no trigger journals every record the sensor sent, with the
  sensor's time and `received_at` (RDG-005, RDG-006).
- The sensor stops and comes back: the session reconnects, and status counts it
  (RDG-007).
- Two copies: only one holds the instrument; the waiting one takes over when the holder
  stops, and the journal has every record once with a gapless `seq` (RDG-008).
- A full journal drops its oldest records for `internal`, and refuses and degrades for
  `restricted` (RDG-009).
- Diagnose names the first failing rung for each case in RDG-010.
- Revoke keeps both journals; install writes a unit pinned to the state directory
  (RDG-011).
- A trigger sees every record in order; after a restart it resumes at the next record;
  a trigger that sleeps does not delay the journal (RDG-012).
- Triggers from a non-portfolio distribution are refused before import; one declared
  twice is used by neither; `on` and reload work as for video (RDG-013).
- Events are journaled per instrument and read back by `events` (RDG-014).
- Every verb reaches its skill, the reads are tools, and no verb's output names a
  credential (RDG-015).
- `instrument list` shows video sources and readings instruments together (RDG-016).

Video's tests run unchanged after the move (RDG-017), and `instrument/tests/` gains a
test per shared module that binds it to a fake kind.

## Decisions

- ADR-275: the kind, the connector and its channel map, source kinds, capture timed by
  the sensor, the overlapping switch, triggers on the journal, the shared event shape,
  and what moves under `instrument`.
- ADR-256: the noun and its kinds. ADR-240, ADR-264, ADR-267: the trigger contract.
- ADR-206, ADR-265: a reading's identity and its time.

## Open questions

- A source kind for a data-acquisition chassis, which reads blocks of hardware-timed
  samples.
- Sending the journal to an ingest point: a transmitter cursor on the same journal.
- One `instrument events` read across every kind.
