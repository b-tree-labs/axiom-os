# ADR-118: SKILL.md Files Are Generated AND Committed, Behind a Drift Gate

**Status:** Accepted (2026-09-19, decided by Ben — "1" on the three-option
framing)
**Related:** ADR-063 (SKILL.md as generated artifact — refined, not
reversed), ADR-051 (the context-file precedent this copies), AEOS
§4.6/§5.3, lint rule AEOS061, ADR-117 (TIDY owns skill conformance — Accepted 2026-09-19), the
frontmatter-contract fix (`fix/skill-md-one-frontmatter-contract` — a
hard prerequisite)

## Context

ADR-063 made SKILL.md a generated artifact so skill code stays the single
source of truth. The conformance census then surfaced the cost: 53
declared skills have no SKILL.md on disk (AEOS061), which breaks three
consumers at once — AEOS §4.6's interop claim ("an extension's
`skills/<name>/SKILL.md` files are valid standalone skills") is false on
disk, external tooling and coding harnesses browsing the repo see
nothing, and the runtime's own SKILL.md reader reads nothing. Meanwhile
the identical tension was already solved once: per-harness context files
are generated from AGENTS.md, committed, marked, and drift-checked in CI
(ADR-051 pattern, shipped).

Three options were framed: (1) generate-and-commit with a drift gate,
(2) teach lint that a skill is merely *generatable*, (3) hand-author
(repeal ADR-063).

## Decision

Option 1. Skill code remains the single source of truth (ADR-063
stands); the generated SKILL.md files are **committed**, carry the
generated-artifact marker, and a drift check makes a stale or
hand-edited copy a CI failure — the exact contract context files
already live under.

Sequencing is part of the decision: **no file is generated until the
frontmatter-contract fix merges.** The current writer emits YAML that
ordinary punctuation breaks (a colon makes a skill invisible; a comma in
a tool name silently widens `allowed_tools`) — committing 53 files from
that writer would bake the defect in 53 times. The defect is quantified: through the old writer, 42 of the 178
skill descriptions shipped in builtin manifests (23%) produced
unreadable YAML — those skills vanish from discovery. The sweep
regenerates exclusively through the fixed single-owner module:
`skills_emit.emit_md_for_spec(spec, out_dir, ext_version)` (delegating
to `skill_md.render_skill_md`), never a second caller path.

## Options considered

- **Teach lint about generatability (2).** Rejected: makes the §4.6
  interop claim conditional on a build step, leaves external readers and
  the runtime reader with nothing, and turns "the file exists" into "the
  file could exist" — a check that verifies intention rather than state.
- **Hand-author (3).** Rejected outright: splits the source of truth
  ADR-063 unified.

## Consequences

- AEOS061's meaning upgrades from "SKILL.md present" to "present AND
  matches regeneration" (drift = failure), mirroring `axi context check`.
- Generated documents self-identify: every emitted file carries
  `generator: axi-skills-emit-md` (constant `skill_md.GENERATOR_MARKER`)
  and the reader exposes `SkillDocument.is_generated`. The drift gate
  regenerates ONLY where `is_generated` is true; an unmarked
  (hand-written or imported) SKILL.md at a managed path is
  validate-parse-only and a reported conflict, never overwritten — the
  ADR-117 §2 mechanism that keeps an authored document and a projection
  from sharing a filename with opposite authority.
- The AEOS061 sweep adds ~53 generated files under the extension trees
  plus the sync/check wiring; it launches when the prerequisite merges.
- §4.6 becomes true on disk; the AAIF-facing story ("our skills are
  valid agentskills.io skills") is inspectable rather than asserted.
- One more generated-artifact surface to keep honest — accepted, because
  the context-file pattern has already proven the maintenance cost is a
  CI line, not a process.
