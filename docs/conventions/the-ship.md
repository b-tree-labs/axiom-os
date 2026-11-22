# The ship

**Status:** Living. Adopted 2026-10-31 as the house figure for a deployed node
and the agents that run on it.
**Related:** [the-axiomatic-way](the-axiomatic-way.md) §6–7 (the REPL and
canonical agent identity), [terminology-ledger](terminology-ledger.md),
[ADR-230](../adrs/adr-230-keel-is-the-one-node-agent.md) (KEEL),
[ADR-215](../adrs/adr-215-a-deployment-has-one-size-and-every-size-sees-the-same-cloud-interfaces.md) (sizes),
[ADR-170](../adrs/adr-170-a-release-is-proven-in-an-idle-slot-before-traffic-moves.md) (blue/green),
[ADR-119](../adrs/adr-119-fleet-console-push-model.md) (fleet),
[prd-agents](../prds/prd-agents.md) §Naming.

## Why a figure at all

The platform is named after a ship. The agents' origin story in
[prd-agents](../prds/prd-agents.md) is a vessel kept running by its crew while
the people aboard get on with their lives. For two years the design grew parts
that are ship's parts under other names: two deployment slots that take turns
(watches), a console that reads reports from nodes it never boards (a flagship
and its fleet), configuration that creeps off its tracked source (drift),
measured, estimated and forecast values (a fix, dead reckoning, a passage plan).
Naming the figure makes those parts explain each other, gives new things a
place to be named from, and gives partners who distrust the word "agent" a
plainer one: a crew member with a job.

A figure is a lens, not a renaming programme. Working agents keep their names.
CLI nouns and verbs stay plain English (`node drift`, not `node set-and-drift`).
The ship's words live in personas, in explanations, in partner-facing text and
in new names.

## The vessel

| Ship | Axiom | Where it is decided |
|---|---|---|
| a ship | a deployed node | ADR-164 (node functions), ADR-019 (profiles) |
| the carrier | `large`: the platform node that launches and hosts everything else | ADR-215 |
| the frigate | `medium`: a full node for one site, often a shared machine | ADR-215 |
| the cutter | `small`: a laptop or single-box node | ADR-215 |
| the powered raft | `micro`: moves under its own power and carries one job; a partner collector, later an embedded board | ADR-215 |
| the buoy | a **role**, not a size: any node fitted only to measure and transmit (the emitter role), whatever hull it sits on | ADR-164 |
| the station | a site: a host node and the buoys that report to it (size is the hull; role is what the hull is fitted for) | ADR-050, site model |
| the fleet | every node that pushes signed reports out to a console that never reaches in | ADR-119, `fleet` |
| home port, dry dock, sea trials | production, staging, the rehearsals before a live change | ADR-182 |
| port and starboard watches | the two blue/green slots; one is always on deck | ADR-170 |
| set and drift | configuration that has moved off its tracked source, or disagrees with itself | ADR-230, `node drift` |
| soundings | vitals: disk, memory, GPU, temperatures; sounding the well is checking the bilge | `node vitals` |
| a fix, dead reckoning, the passage plan | a measured value, an estimated one, a forecast | the twin signal interface |
| the log | the attested record (the Log surface) | ADR-150 |
| the ship's papers | what a node carries to be handed over: identity, inventory, certificate, recent log. The command is the plain `node report` | ADR-230 (planned) |
| continuous survey, a class certificate | continuous certification (ConCert): surveyed continuously, certified through a date, instead of audited once | prd-work-verification |
| carrying others' cargo | a shared machine: other accounts can fill the hold, so the keel is watched | ADR-230 (KEEL default on medium shared nodes) |

## The crew

Agent identities are canonical (the-axiomatic-way §7); this column says what
each one is aboard, in one line, for the persona and for a colleague meeting
the roster for the first time.

| Agent | Aboard |
|---|---|
| AXI | the officer of the watch: the one you speak to, who relays the captain's intent to the rest |
| KEEL | the structure: keeps the vessel upright and on course; inventory, soundings, drift, upkeep (ADR-230) |
| RIVET | the shipwright: builds, refits and releases the vessel; a rivet is a hull term |
| KEEP | the strongroom: holds the keys and knows when each one expires (ADR-209) |
| GUARD | the gangway watch: who comes aboard and what they may touch |
| HERALD | signals: flags, lamps and the bell; what is said to whom |
| CLERK | the captain's clerk: the programme, the follow-ups, the correspondence |
| PLINTH | the hold: what is carried, in which tier, how fresh |
| WARDEN | the port warden: what crosses between vessels and harbours (federation) |
| PULSE | the watch bill: who is on duty when |
| SCAN | the lookout |
| CURIO | the ship's naturalist: the research voyage's scientist |
| PRESS | dispatches |
| CHALKE | the schoolmaster |

TIDY and TRIAGE fold into KEEL (ADR-230) and leave the roster when their skills
have moved. CURIO (specified, not shipped) is the next merger candidate: into
CHALKE, Keplo's agent, or into AXI's Eval phase; open as of 2026-10-31.

## Rules

1. **Plain verbs, nautical explanations.** A command is named for what it does
   in the words a new operator would type (`inventory`, `vitals`, `doctor`,
   `drift`, `clean`). The figure explains the command; it does not name it.
2. **New names come from the ship.** A new agent, size, surface or state looks
   here first. A name from outside the figure needs a reason in its ADR.
3. **Partner text uses the figure where it removes a scarier word.** "A buoy:
   it measures and transmits" replaces "an emitter node running three agents".
4. **No weapons.** The carrier is a platform that launches and hosts; nothing
   in the vocabulary is armed.
5. **Don't rename for the figure.** KEEP and PLINTH are not nautical and stay.
   A rename is justified by confusion, never by consistency with a metaphor.

## Terms introduced by this figure

Recorded in the [terminology ledger](terminology-ledger.md) under "The ship":
*buoy*, *drift*, *soundings*, *sea trials* (plain) and *continuous survey*
(introduced once). The figure is not slavish: where a standard word exists
(`report`, `inventory`, `doctor`), the standard word wins.
