# ADR-230: KEEL is the one agent for the machine a node runs on

**Status:** Accepted (2026-10-31)
**Related:** ADR-056 (CLI nouns are purpose-named; verbs are skill functions), ADR-140 (local models are two roles on one family), ADR-119 (the fleet console never reaches in), ADR-209 (KEEP owns expiry), spec-aeos-0.1 §5, `docs/specs/spec-llm-tier-policy.md` §2 (the alignment check)

## Context

On 2026-10-31 a production node had been short of disk for a day and had lost its
database for two hours the day before. The questions that had to be answered to
put it right were ordinary ones: what is using the disk, by tier and by owner;
which engines serve which models, from which weights, on which ports, through
which gateway; which of those are the ones the code pins; and which of the
node's configuration files are tracked somewhere and which are hand edits. No
agent could answer any of them. The answers came from `ps`, `ss`, `du`,
`nvidia-smi`, `systemctl cat` and `nginx -T` run by hand, one at a time.

The capability was not missing; it was scattered. TIDY (`hygiene`) owned disk,
retention and vitals. TRIAGE (`diagnostics`) owned health checks and diagnosis.
`install_health` owned `doctor`. `llm_serving` owned `serving status` and
`serving diagnose`, which saw only the engine it had installed; the engine that
mattered ran under another account from a hand-written script and was invisible
to all four. The model the serving env named, the model the ensure unit pulled
and the model the code pinned had disagreed for weeks, and nothing compared them.

Three agents and a half, each with a noun, a persona, a skill list and a test
suite, add up to more surface than one person can hold in their head and less
capability than one agent with the whole picture. None of the nouns has users
yet: the platform is still being brought up. That is the moment to merge, before
anyone has to be migrated.

## Decision

**One agent, KEEL, owns the machine a node runs on and everything that runs on
it.** The name is the ship's spine: the part that keeps it upright and on
course, and "on an even keel" is the state it reports. It is the first name
taken from [the ship](../conventions/the-ship.md), the house figure this
decision also adopts: a node is a vessel (a carrier, a frigate, a cutter or a
powered raft by ADR-215 size; a buoy by role), the agents are its crew, configuration that moves off
its source is drift, vitals are soundings.

KEEL absorbs TIDY (`hygiene`), TRIAGE (`diagnostics`) and the `install_health`
doctor, plus the local-model alignment check. Its CLI noun is `axi node`, purpose-
named per ADR-056, with five verbs, each a skill function:

| verb | answers | reads / changes |
|---|---|---|
| `node inventory` | what runs here: engines, units, listening ports, served models, weights and their location, disks by tier and owner. Engines run outside Axiom are **declared** (unit, port, weights directory, owner) so they are seen, not guessed at. | reads |
| `node vitals` | disk, GPU, CPU, memory, temperatures, and the watch timer that alerts through HERALD | reads |
| `node doctor` | the health checks and their fixes, in plain English; the `install_health` doctor and the diagnostics checks become one list | reads |
| `node drift` | where the node disagrees with itself or with what is tracked: code pins vs the ensure unit vs the serving env; a live configuration file with no tracked source; a unit the repo does not know | reads |
| `node clean` | retention, orphaned resources, archive to a bulk tier; proposes, then acts on approval | changes, gated |

The old nouns are **removed, not aliased**: nobody uses them, and an alias is a
second name that has to be explained forever. `fleet` is not absorbed; it is the
cross-node console and, per ADR-119, never reaches into a node. `llm_serving`
keeps `serving` for installing and operating the stack it owns; `serving
diagnose` calls `node doctor` for the checks they share.

## Options considered

- **Merge TIDY and TRIAGE only.** Leaves `doctor` and `serving diagnose` as two
  more places a person has to think to look. The failure that forced this was
  exactly that the picture was in pieces.
- **Merge everything node-local, including `fleet`.** `fleet` answers a different
  question (which nodes report, and what drifted between them) from outside the
  node, by design. Folding it in would blur the one boundary ADR-119 drew on
  purpose.
- **Keep the agents, add a cross-cutting "inventory" skill.** Adds a fourth place.
  The problem is the number of places.
- **Names.** BOSUN fits the persona pattern (HERALD, WARDEN, CLERK) but is a
  monitoring product's name in the same domain. STEWARD is accurate and
  forgettable. ATLAS is three other products. KEEL is short, unclaimed in this
  space, and says what the agent is for.

## Consequences

- One extension, `src/axiom/extensions/builtins/node/`, one persona, one roster
  entry, one place to look. The `hygiene`, `diagnostics` and `install_health`
  extensions are deleted once their skills have moved; their tests move with
  the skills, and the guard list in `tests/test_axiom_names_no_domain.py` loses
  their lines.
- Inventory needs a **declaration** for anything that runs outside Axiom's own
  units. That is new, and it is the right requirement: a node that cannot say
  what runs on it is not a node anyone can hand over.
- `drift` turns today's hand-made comparisons into rules: pin vs unit vs env
  (shipped in the alignment check), tracked vs live for configuration files and
  units. Each rule is one function and one fixture.
- Order of work: this ADR and the roster entry; `node inventory` and `node
  drift` (the two verbs needed on 2026-10-31); `node doctor` and `node vitals`
  (fold, do not rewrite); `node clean`; then delete the old extensions.
- The persona count goes down by two and a half. That is the point.
