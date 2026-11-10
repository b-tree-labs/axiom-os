# PRD: Action Rehearsal (program feature 6 — "robot twins for the common builder")

**Status:** Living. **Spec:** `spec-action-rehearsal.md`.
**Lineage:** the robot-twins plan (`docs/working/robot-twins-plan-2026-09-19.md`)
scaled to the everyday builder; the six abstractions (twin-of-record,
rehearsal-before-actuation, graduated authority, pinned floor,
receipted episodes, unit→fleet) are its spine.

## The common-builder translation

The robot-twins thesis says automation earns authority through its
twin. The common builder's version is smaller and universal: **before
your agent runs the irreversible thing tonight, it runs the exact same
commands against a cheap twin of your reality — and the rehearsal
receipt rides the approval.** Not physics simulation; that's the trap
that kills digital-twin products and it stays in the domain track. The
common builder's twin is RECORDED REALITY: a container snapshot, a
database fork, a VCR cassette of an API, a `--dry-run` capture. The
market adjacency is direct: destructive-ops-with-no-undo is a
505-reaction open issue, and whole tools now exist just for agent
command undo. Undo is the apology; rehearsal is the manners.

## Users

- A developer whose agent is about to touch prod: deploy, migration,
  bulk delete, `kubectl apply`, DNS.
- A builder whose agent drives a physical thing where "undo" is
  meaningless: 3D printer, CNC, drone, home automation (the Omarchy
  hobbyist overlap is large).
- The approval flow: a human approving a risky action WITH the
  rehearsal receipt attached is the anti-approval-fatigue design the
  enterprise slice asked for ("meaningful oversight, not
  rubber-stamp").

## Requirements

### R1 — Twins are recorded, not simulated
v1 twin providers: container/filesystem snapshot, Postgres
fork-restore (the backup-validate scratch machinery IS this),
HTTP-API cassette (record/replay), and dry-run capture where tools
natively offer it. A physics/behavioral simulator is explicitly out
of scope — that is the domain layer's business.

### R2 — Rehearse the ACTUAL commands
The rehearsal replays the agent's real command stream (from the
dispatch record) against the twin — not a test suite, not staging's
last deploy. Staging tests the code; rehearsal tests *tonight's
specific commands by this specific agent*.

### R3 — Rehearsal receipts ride approvals
A rehearsal produces a receipt (what ran, what changed in the twin,
diff attached — Proof-of-Done bindings in the twin). Approval
surfaces render it: "approve the real run" shows the rehearsal
evidence face-up. Unrehearsed risky actions still say so, honestly.

### R4 — Graduated authority per target
Each target (this DB, this cluster, this printer) carries an
authority level: shadow (rehearse only) → propose (rehearse + human
approves real) → act (rehearse + auto-proceed on clean receipt).
Promotion between levels is a human act, receipted. The pinned floor
never graduates (safety-class actions always propose).

### R5 — Launch platforms and scope
macOS + Omarchy first; v1 targets = shell/deploy, Postgres, HTTP
APIs. Physical-device twins arrive only as recorded-protocol
cassettes (no device simulation claims).

## Non-requirements (v1)

- No physics or behavioral simulation of anything.
- No promise of fidelity beyond the twin's recording ("the rehearsal
  passed" means passed AGAINST THE SNAPSHOT — copy says so).
- No automatic rollback (rehearsal reduces the need; undo tools
  remain complementary).

## Red-team

**Steelman.** Universalizes the program's deepest idea (authority
earned through evidence) to a pain every builder has felt; reuses the
scratch-restore machinery, dispatch records, PoD bindings, RACI
gates, and receipts; uniquely honest positioning ("we rehearse your
actual commands" vs staging's "we tested some code once"); and it is
the anti-approval-fatigue answer the enterprise slice explicitly
described.

**Strawman.** (1) The digital-twin trap: fidelity promises rot and
users blame the product when reality diverges from the snapshot.
(2) "We have staging" — the differentiation is subtle and will be
missed. (3) Twin setup cost: if making a twin takes longer than the
risky action, nobody rehearses. (4) Scope explosion into
simulation-land is one enthusiastic contributor away.

**Design responses.** (1)→R1+non-req #2: recorded-reality only, and
the receipt names the snapshot's age — divergence is rendered, never
hidden ("rehearsed against 2h-old fork"). (2)→the copy leads with the
sentence that survived red-teaming: *staging tests the code;
rehearsal tests tonight's commands.* (3)→v1 targets are chosen
because their twins are ONE COMMAND (pg fork via existing scratch
machinery; container snapshot; cassette record) — twin cost is the
feature's real gate and the spec budgets it per target. (4)→the
non-requirement is written in the PRD precisely so scope cops can
cite it.

## Success criteria / kill test

Success: our own riskiest recurring action (node deploys or a DB
migration) runs shadow-mode rehearsals for 30 days; ≥1 rehearsal
catches a would-have-failed run (diff or error in the twin) before
reality; the approval card with rehearsal receipt ships in the
Receipts surface.
Kill: if 30-day dogfood shows rehearsals passing 100% and never
changing an approval decision, the twin targets are wrong or the
risky actions aren't — reassess targets before widening.
