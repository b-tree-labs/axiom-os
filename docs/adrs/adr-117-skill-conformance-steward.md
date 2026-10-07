# ADR-117 — TIDY owns skill conformance: synchronize, repair, validate — never author

**Status:** Accepted — 2026-09-19 (decided by Ben: accept as written)
**Owner:** @ben
**Related:** ADR-063 (SKILL.md as generated artifact), ADR-056 (skill-as-function),
ADR-032 (standards positioning), spec-aeos-0.1 §4.6/§5, lint rule AEOS061

## Context

A skill is described in three places: the `SkillRegistry` registration (Python),
the `[[extension.provides]] kind = "skill"` block in the AEOS manifest, and
`SKILL.md`. ADR-063 named the registry the single source of truth and made
SKILL.md a generated, committed artifact with a CI equivalence check.

Two things happened since that the decision did not anticipate.

**A second direction appeared.** `infra/skill_md.py` now *reads* a SKILL.md,
turning a hand-written procedure into a registered capability. ADR-063 assumed
one direction only. With both live, the same filename means two incompatible
things: a projection that must not be edited, and a source of truth that must
not be overwritten.

**Nothing watches any of it.** AEOS061 ("skill declared but SKILL.md missing")
sits at `warn` with 53 findings, and no agent invokes `axi ext lint` on any
cadence — the hygiene extension never calls it. Conformance is therefore
"someone remembers to run the linter." That is why 53 findings accumulated
unnoticed, and why a writer that emitted **unreadable YAML for 23% of shipped
skill descriptions** (a colon in a description is invalid YAML) survived until
someone went looking.

The capability gap is not tooling. `axi skills emit-md`, `axi ext lint` and
`axi ext doctor` all exist. What is missing is an **owner** who runs them,
proposes repairs, and is accountable for the surface staying true.

## Decision

**TIDY owns skill conformance**, as a `stat skills` surface alongside the
drift and doc-standards surfaces it already stewards, using the existing
propose → approve loop.

TIDY performs exactly three verbs, and explicitly not a fourth:

| Verb | Applies to | What it does |
|---|---|---|
| **synchronize** | generated docs | assert `SKILL.md ≡ render(SkillSpec)` and manifest `provides` ≡ registry |
| **repair** | generated docs | regenerate through the single contract module and **propose** the diff |
| **validate** | imported docs | assert it parses, its name is unique, its `allowed-tools` resolve |
| ~~improve~~ | — | **out of scope.** Rewriting a description is authorship, not hygiene |

Improvement is excluded deliberately. A hygiene agent that rewrites prose is one
people turn off, and the moment TIDY authors content it becomes a source of
truth rather than a steward of one.

### Ownership is per file, declared in the file

A generated document carries a provenance key in its frontmatter:

```yaml
generator: axi-skills-emit-md
```

- **Marker present** → generated. TIDY asserts equivalence and may propose a
  regeneration. Hand edits are drift and will be proposed away.
- **Marker absent** → authored (hand-written, or imported from another harness).
  TIDY validates it and **never regenerates it**.

This resolves the ADR-063 ambiguity without giving up either direction, and it
is what makes termination provable below.

## Termination — why TIDY cannot loop

A steward that repairs what it observes can oscillate. Each mechanism below is a
stated invariant with a test that holds it, not a hope.

**1. Generation is a pure function.** `render(SkillSpec, ext_version)` depends on
nothing else — no timestamp, hash, ordering or environment. Therefore
`render(render⁻¹(x)) == x` and a repair converges in one step instead of
producing a fresh diff each pass. *Verified today* (rendering twice is
byte-identical, no volatile tokens) and pinned by an idempotence test.

**2. One direction owns each file.** The ping-pong risk is specific to this
design: read an authored doc → register a spec → regenerate the doc from that
spec → the doc differs from what the author wrote → repair → forever. The
provenance marker forecloses it. An authored document is never an input to
generation, so the cycle has no edge to traverse.

**3. Detection never mutates.** The `stat skills` check is read-only. Repairs
are applied by the approval path, by a human decision. A detector that cannot
write cannot feed itself.

**4. A denied proposal is not re-raised.** Proposals are keyed by
`(skill, finding-kind, content-hash)`. A denial suppresses that key until the
underlying content changes — the key changes, so a genuinely new problem is
still reported. This is the propose → ask → back-off posture applied to a
recurring check rather than a one-shot action.

**5. Self-application terminates.** TIDY's own skills are in scope; excluding
them would be the wrong kind of exception. Convergence follows from (1) and (3):
TIDY's repair of TIDY is idempotent and applied externally. A bounded pass count
per trigger is a backstop, not the mechanism.

**6. The heartbeat does not re-enter.** The check runs under the existing
single-flight lease and never enqueues itself. One pass per trigger, bounded
work per pass.

**The fixed-point test is the guard:** run detect → apply every proposed repair
→ detect again, and assert the second detection is **empty**. A check that
cannot reach a fixed point fails its own suite before it ever runs against a
repository.

## Consequences

**Good.** The three-way description becomes one authored copy per skill with the
rest derived and *watched*. AEOS061 gains an owner, which makes the ADR-063
question answerable: a drift check somebody runs is a different proposition from
one nobody runs. Imported skills get a conformance story they currently lack.

**Costs.** TIDY grows a surface, and generated SKILL.md files become
reviewable artifacts in the tree. Hand edits to generated documents will be
proposed away — intended, and the marker says so in the file.

**Known gap this exposes.** TIDY's own `skills/` package contains only
`__init__.py` while its manifest declares `path = "agents/tidy/skills/*.md"`.
The agent that would police skill conformance does not currently satisfy it.
Fixing TIDY's own declaration is the first task under this ADR, not a follow-up.

**Not decided here.** Whether generated SKILL.md files are committed (ADR-063's
open question) is left open deliberately: it turns on whether the AEOS donation
track is live this quarter per ADR-032, which is a strategic call. This ADR
makes either answer safe by naming an owner and proving termination.
