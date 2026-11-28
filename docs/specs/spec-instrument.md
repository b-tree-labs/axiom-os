# Spec: Instruments

**Owner:** B-Tree Labs • **Status:** Draft • **Last updated:** 2026-11-22
**PRD:** [prd-instrument.md](../prds/prd-instrument.md) • **Key ADRs:** [ADR-256](../adrs/adr-256-an-instrument-quantifies-what-a-sensor-gives-and-kinds-register-under-one-noun.md), [ADR-275](../adrs/adr-275-readings-is-the-instrument-kind-for-quantities-with-a-unit-and-what-two-kinds-share-moves-under-instrument.md), [ADR-243](../adrs/adr-243-people-see-a-log-code-keeps-logbook-and-log-is-the-cli-noun.md), [ADR-240](../adrs/adr-240-a-video-source-is-a-connector-with-a-kind-and-a-trigger-is-a-registered-contract.md), ADR-023-A1

## Overview

The `instrument` extension (`src/axiom/extensions/builtins/instrument/`) owns the
command noun `instrument` and a registry of instrument kinds. A kind is another
extension's, or a consumer package's, contribution: it registers through the manifest
block `[[instrument_kinds]]` or the entry-point group `axiom.instrument_kinds`, behind
the portfolio gate. `instrument list` and `instrument status` summarise every kind;
`instrument <kind> <verb>` hands the command line to the kind. The kinds are `video`
([spec-video](spec-video.md)) and `readings` ([spec-readings](spec-readings.md)). Each
keeps its own extension, skills, capture service and data path, and uses the code the
kinds share (§What the kinds share).

| Layer | Owns | Does not own |
|---|---|---|
| `instrument` | the noun; the kind registry and its gate; `list`, `status`; the verb-to-skill map; the code the kinds share (`shared/`) | running capture; frames; what a trigger observes; any journal, claim, boot unit or connector kind of its own |
| a kind (`video`, `readings`) | its verbs, flags, output and skills (`video.*`); its data path; its dependencies; its uncertainty posture | the noun |

## Contracts

### `InstrumentKind`

```python
@runtime_checkable
class InstrumentKind(Protocol):
    name: str                          # the word after `instrument`: "video"
    description: str                   # one line, for `instrument list --kinds`
    yields: str                        # what it quantifies into: "frames", "readings"
    skill_for_verb: Mapping[str, str]  # {"add": "video.add", "list": "video.list", ...}
    def build_parser(self, prog: str) -> argparse.ArgumentParser: ...  # its verbs, for help and Tab
    def main(self, argv: list[str], prog: str) -> int: ...             # runs one verb; returns the exit code
    def list(self) -> list[InstrumentRow]: ...                         # one row per declared instrument
    def status(self) -> KindStatus: ...                                # one summary for the kind

@dataclass(frozen=True)
class InstrumentRow:
    kind: str           # "video"
    name: str           # "cam-1"
    health: str         # ready | degraded | failed | unmounted (ADR-220 §3)
    state: str          # the kind's own word: "capturing", "stopped", ...
    detail: str = ""    # one line; never a credential

@dataclass(frozen=True)
class KindStatus:
    kind: str
    ok: bool
    summary: str                         # one line: "2 sources, 1 capturing, 1 degraded"
    counts: Mapping[str, int] = field(default_factory=dict)   # by health
    error: str | None = None
    fix: str | None = None               # names the command the person typed
```

That is the whole contract. It assumes no frames, no bus, no triggers, no source kind
and no connector file, so `readings` fits it unchanged.
`prog` is the full command path the person typed (`acme instrument video` under a
product branded `acme`), so a kind's usage lines name it. A kind's fixes name the same
path; video builds it in one helper beside `naming.cli_name()`, and writes it out in
full in its precondition templates, where the platform fills only `{cli}`. A kind's
module must import without its heavy dependencies: video's kind object imports no
`numpy` and runs no `ffmpeg`.

### Triggers and events (ADR-256 D7)

Triggers and events extend to every kind. Two halves:

- **The event is shared.** One kind-neutral shape, the fields of video's `TriggerEvent`
  (spec-video §`StreamTrigger` and `TriggerEvent`) with the source named as the
  instrument: instrument, trigger (`name@version`), `ts`, `kind` (`started` | `ended` |
  `occurred`), `incident_key` (`<instrument>:<trigger>@<version>:<episode>`), `severity`
  (from the trigger's own configuration, default `info`), `score`, `detail`. It never names
  a recipient (ADR-238 D2). The class is `TriggerEvent` in `shared/events.py`; its
  `stream` field is the instrument. Each instrument's events go to a DAQ journal of
  their own under its kind's directory, read by the kind's `events` verb (ADR-275 D7).
- **What a trigger observes is per kind.** Video's `StreamTrigger.observe(frame)` sees
  `Frame`s; a readings trigger's `observe(record)` sees `Record`s. Data paths stay
  separate (D6). Discovery is gated the same way for every kind: each names its own
  manifest block and entry-point group (`[[video_triggers]]`, `[[readings_triggers]]`).

The `InstrumentKind` contract stays free of triggers: a kind offers them; the noun does
not require them.

### What the kinds share

`src/axiom/extensions/builtins/instrument/shared/` holds the code video and readings both
use (ADR-275 D8). It is a library, not part of the kind contract, and it holds code, not
data: each kind runs it under `<state>/<kind>/` with its own names.

| Module | Holds |
|---|---|
| `events.py` | `TriggerEvent`, `incident_key`, `EVENT_KINDS`, `SEVERITIES`, the defaults |
| `triggers.py` | `TriggerConfigError` and its kinds, `split_attachment`, `severity_of`, `on_entries`, `TakesEvents`, `BaseTrigger`, `TriggerEntry`, `TriggerCatalog`, gated discovery for a `TriggerFamily` (its kind, manifest block, entry-point group and command), checking a set of attachments (a name attached twice, an `on` on itself, on nothing, or in a cycle), and closing an episode its trigger cannot close |
| `ledger.py` | an event journal per instrument under a kind's root: one writer at a time across processes, a torn last line repaired, read by time across instruments |
| `state.py` | `InstrumentState(kind)`: claims, started marks, verify records, live and standby snapshots, reload requests, open episodes and logs under `<state>/<kind>/` |
| `control.py` | starting and stopping a kind's capture process, and handing an instrument to a running `--all` service |
| `boot.py` | a kind's boot unit (`<kind>-capture`) and its advertised `--all` services |
| `ladder.py` | the diagnose ladder: the first failing rung names its fix, the rest are skipped |
| `backoff.py` | the reconnect back-off |

Video binds each to `video` (`<state>/video/`, `[[video_triggers]]`,
`axiom.video_triggers`, `video-capture`, `AXIOM_VIDEO_CLI`). Its modules keep every name
they export, and what it writes does not change.

### Declaring a kind

The manifest block, at the manifest's root:

```toml
[[instrument_kinds]]
name = "video"
entry = "axiom.extensions.builtins.video.instrument:KIND"
```

or the entry point, in a portfolio distribution's packaging:

```toml
[project.entry-points."axiom.instrument_kinds"]
gauges = "consumer_pkg.gauges:KIND"
```

`entry` resolves to an `InstrumentKind` or a zero-argument callable that returns one.
The resolved object's `name` must equal the declared name. `aeos-manifest-0.1.json`
gains a root-level `instrument_kinds` array (`name`, `entry`; no other keys), beside
`video_triggers`.

**Names.** `^[a-z][a-z0-9-]{0,23}$`. Reserved, because they are the noun's own verbs:
`list`, `status`, `help`. A reserved or malformed name is refused with the reason.

**Gate.** As video triggers (`video/triggers.py`): a manifest in Axiom's builtins
directory is trusted; any other manifest, and every entry point, is trusted only when
its distribution declares `axiom.portfolio_member`
(`portfolio_distributions()`, ADR-023-A1 A1.3). The decision is made from where the
declaration lives, before anything is imported.

**Duplicates.** A name declared by two origins is used by neither; both origins are
named in `instrument list --kinds` and in the log, with the fix ("uninstall one, or ask
its owner to rename the kind"). In-process registration (`register(kind)`, used by
discovery after the gate and by tests) sits on `axiom.infra.connector_registry.ConnectorRegistry`
(ADR-110 §1): a different object under a live name raises; the same object again is a
no-op.

### CLI `<cli> instrument`

| Command | Skill | Output |
|---|---|---|
| `instrument list [--json]` | `instrument.list` | one row per instrument, every kind: kind, name, health, state |
| `instrument list --kinds [--json]` | `instrument.list` (`kinds=true`) | each kind: name, yields, origin, description, state (`registered`, `refused`, `conflict`, `broken`) and the reason |
| `instrument status [--json]` | `instrument.status` | one `KindStatus` per kind |
| `instrument <kind> <verb> ...` | the kind's (`video.<verb>`) | the kind's |

Both skills are reads (`risk = "read"`, surfaces `cli`, `mcp`, `agent_tool`).

**Dispatch.** The platform dispatcher calls `instrument`'s `main()` with
`sys.argv = ["<cli> instrument", *rest]` (`axiom_cli.main`, then `_dispatch_extension`). `main` reads
`rest[0]`:

- a reserved word runs the noun's own verb;
- a registered kind runs `kind.main(rest[1:], prog=f"{cli} instrument {name}")` and
  returns its exit code;
- a kind that is refused, conflicting or cannot load exits 1 with the reason and the fix;
- any other word exits 2: `no instrument kind named 'vidoe' (registered: video). Did you
  mean: <cli> instrument video?`, and `<cli> instrument list --kinds`;
- nothing, or `--help`, prints the noun's verbs and the registered kinds.

`<cli>` is the active brand's program name (`get_branding().cli_name`), never a literal
`axi`.

**The verb-to-skill map.** `instrument`'s command module exports `SKILL_FOR_VERB`, the
map ADR-243 introduced for `log`: `{"list": "instrument.list", "status":
"instrument.status", "video add": "video.add", ...}`, assembled from each registered
kind's `skill_for_verb`. `cli/command_space.py` reads a noun's exported map before it
joins words, so `instrument video status` gets skill `video.status` and risk `read`.

**Manifest of `instrument`.**

```toml
[[extension.provides]]
kind = "cmd"
noun = "instrument"
entry = "axiom.extensions.builtins.instrument.commands:main"
description = "Instruments: what this node quantifies from its sensors (video first)"
tier = "core"
intent_groups = ["operate"]
functions = ["acquire"]

[extension.uncertainty]
posture = "not-applicable"
```

plus a generated `[[extension.provides]] kind = "skill"` block per skill
(`axi skills emit-md --ext instrument`).

**What the first kind changes.** `video` drops its `cmd` provider (and with it
`functions`, `tier` and `intent_groups`, which the noun now carries) and declares
`[[instrument_kinds]] name = "video"`. Its verbs, flags and skills are unchanged; every
fix, precondition and document says `<cli> instrument video <verb>`. Connector kind
`video`, `source.kind = rtsp`, the capture service, the frame bus, triggers, `motion`, the
ledger, `axiom.video_triggers`, `<state>/video/`, `AXIOM_VIDEO_*` and the doctor check
do not change. `AXIOM_VIDEO_CLI` still carries the program name only, because the video
extension also writes fixes that name other nouns (`<cli> vault set`).

## Design

**Discovery.** On first use in a process, `instrument` scans the `[[instrument_kinds]]`
blocks of every discovered extension (`discovery.discover_extensions()`, as video
triggers do) and the `axiom.instrument_kinds` entry points, applies the gate, then
imports what passed. That is one manifest scan per `instrument` command; if it shows in
the noun's start-up time, the catalogue joins the startup cache (`index_cache`). The
catalogue (entries, refused, conflicts, broken) is kept for the process. A kind that raises on import, or whose object fails the protocol check, is
`broken` with the error; nothing it raises reaches another kind or the noun.

**Summaries.** `instrument.list` and `instrument.status` call each kind's `list()` and
`status()` in turn. An exception from a kind's `list()` puts that kind under `failed`,
with the error and a fix; one from its `status()` becomes its `KindStatus` with
`ok = false`. A kind that is refused, declared twice or broken is in `status` too, not
ok, with its reason and fix. The others still answer. Video implements both over the
status view its `video.list` and `video.status` skills read.

**Help and Tab.** `instrument`'s `get_parser()` adds one subparser per registered kind,
filled from `kind.build_parser(prog)`; `axiom_cli._copy_subparsers` and
`completion_index.serialize_parser` already copy a tree to any depth.

**Gates.** `acquire` (ADR-164) and the availability gate (ADR-047) apply to the noun, so
to every kind. A kind's own requirements stay preconditions of its skills (video: `ffmpeg`,
`numpy`).

**Platform changes this needs.** Built with the extension:

| ID | Change | Why |
|---|---|---|
| INS-P1 | `command_space.annotate(doc, specs, maps=...)` reads `{noun: SKILL_FOR_VERB}` before it joins words (the mechanism ADR-243 proposed for `log`). `axiom_cli.completion_doc` collects the maps with `exported_skill_maps` from noun modules it has already imported, so nothing is imported to ask | Without it, every kind verb loses its skill and risk, and the `next:` lines skip them |
| INS-P2 | A noun module may export `parser_sources()`. The stored tree records those files' stat tokens (`sources`), and `completion_doc` treats a change as a miss. The fingerprint also covers the directories packages install into (`sysconfig` `purelib` and `platlib`), so installing or removing a distribution, an entry-point kind's included, rebuilds the tree | The fingerprint covered the `cmd` module and the manifests only, so editing a kind's parser, or installing a kind by entry point, left Tab stale |
| INS-P3 | Chat's `/skills` (`chat/commands_ext/skills_cmd.py`) names a skill's command from the annotated tree (`command_space.skill_paths`, shortest path first) and falls back to `<cli> <namespace> <verb>`, saying "by convention" only then | It printed `axi video add` for `video.add` |

Known, accepted for now: the `next:` history keeps the first two words
(`instrument video`), and `cli.command_started` names the noun only.

## Tests

Unit, in `src/axiom/extensions/builtins/instrument/tests/`, written first. The fake kind is
**scalar-shaped** (it yields readings with a unit, has no frames, bus or connector file)
so the contract is tested against the kind that does not exist yet.

- A kind declared in a builtin manifest block registers; the same kind from an entry point
  of a portfolio distribution registers; from a non-member distribution it is refused,
  before import, and `list --kinds` says why.
- A name declared twice is used by neither, and both origins are listed; a reserved or
  malformed name is refused; an entry whose object's `name` differs is not used
  (`broken`, naming both).
- In-process `register`: a different object under a live name raises; the same object
  is a no-op.
- A kind that raises on import is `broken`; `instrument list`, `status` and every other
  kind still work.
- `instrument <kind> <verb> ...` passes the rest of the command line and the branded
  `prog`, and returns the kind's exit code; an unknown kind exits 2 naming the registered
  kinds, the nearest match and the brand's program name.
- `instrument.list` and `instrument.status` are registered, declared, reads, and the only
  verbs of the noun; one kind raising in `list()` or `status()` fails only its own rows.
- Help and Tab: the tree reaches `instrument <fake> <verb> --<flag>`; `command_space`
  annotates the kind's verbs with their skill and risk (INS-P1); the completion
  fingerprint changes when the fake kind's command module changes or an entry-point kind
  appears (INS-P2).
- `instrument --help` and `instrument list --kinds` leave `numpy` out of `sys.modules`.
- Through the real top-level dispatcher (`axiom_cli.main`), in process.
- `test_cli_noun_collisions`: `instrument` has one owner, and `video` provides no noun.

Video's own tests move with it (spec-video §Tests): its fix strings, preconditions and
subprocess tests name `instrument video`.

Each module in `shared/` has a test that binds it to a fake kind under its own state
directory: its files land under `<state>/<kind>/`, its fixes name that kind's command,
and its trigger discovery reads that kind's block and group alone. Video's tests run
unchanged against the bound modules.

## Decisions

- ADR-256: the words; one thin owner of the noun; kinds register behind the gate; the
  kind owns its verbs; skill names follow the kind's extension; share the control
  surface, not the data path or the lifecycle; triggers and events belong to every
  instrument (D7).
- ADR-243: one noun with a registered second word, skill names unchanged, and
  `SKILL_FOR_VERB` for the command-path join.
- ADR-240 D3 and ADR-023-A1: discovery by manifest block and entry point, gated before
  import.
- ADR-110 §1: the in-process registry.
- ADR-164: the noun belongs to `acquire`.
- ADR-275: readings is the second kind; the event shape is shared, each instrument's
  events are journaled under its kind, and what the kinds share is in `shared/`.

## Open questions

- One `instrument events` read across every kind.
