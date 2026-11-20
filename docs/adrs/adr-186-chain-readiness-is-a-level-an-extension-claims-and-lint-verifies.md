# ADR-186: Chain readiness is a level an extension claims and lint verifies

**Status:** Accepted (2026-10-08)
**Related:** [ADR-185](adr-185-tools-declare-what-they-need-and-make-and-vetted-chains-are-published-as-recipes.md) (the declarations and the runtime), [ADR-063](adr-063-skill-md-as-generated-artifact.md) (the capability is the single source), [ADR-032](adr-032-standards-positioning-dual-track.md) (AEOS stays internal), `docs/specs/spec-aeos-0.1.md` (section 4.11, 4.12), `docs/reference/aeos-chaining-prior-art-2026-10.md`

## Context

ADR-185 added optional per-skill declarations (`risk`, `requires`, `produces`,
`next_steps`, `recipes`) and enforced them with a repository-local debt list. That
made them true of this repository and of nothing else: an extension author outside
the repo had no standard text to read, no manifest field to put the declarations in,
no way for `axi ext lint` to check them (lint is static and reads only the manifest),
and no way to say how much of this an extension had done. A standard was needed, not
another guard.

A prior-art survey (2026-10-08) found that no public standard covers the set. The
parts that exist publicly (the MCP annotation hints, `outputSchema` and
`structuredContent`, OpenAPI Link Object wiring, Arazzo workflows, MCP prompts, MRTR
`requestState`) should be used as they are; the rest is the AEOS delta.

## Decision

1. **A separate axis, claimed per extension.** `[extension] chain_level` is `A`
   (baseline, the default), `B` (chain-ready) or `C` (recipe-publishing), distinct
   from Bronze, Silver and Gold. It is an additive minor, **AEOS 0.2 (draft)**,
   carried inside the existing spec until ratified. Existing manifests conform at A
   with no change; a claim of B or C requires `aeos_version >= 0.2`.
2. **Declarations live in the manifest `kind = "skill"` table, as data; the
   `SkillSpec` stays the source of truth.** Lint is static and may not import
   extension code, and an MCP-facing consumer needs the facts without loading it. The
   table is the generated projection (`axi skills emit` writes it), and for built-in
   extensions rule AEOS130 compares table and code so they cannot drift. This also
   resolves a mismatch the spec had: section 4.9.4 showed `side_effects` and
   `surfaces` on the table while the schema rejected them.
3. **Use the public shapes where they exist.** `risk` and `idempotent` and
   `open_world` project onto the MCP annotation hints; `produces` onto `outputSchema`
   and `structuredContent`; an unmet requirement is `isError: true`; follow-ups use
   OpenAPI Link vocabulary with RFC 6901 pointers; vendor `_meta` keys are
   reverse-DNS (`ai.axiom/chain`); recipes are MCP prompts with an Arazzo-compatible
   projection. An undeclared `risk` leaves `destructiveHint` unset, never "harmless".
4. **Stake the delta as rules with ids and checks that can fail.** Call-time
   `requires` with a mandatory exact `fix`; result-embedded `next_steps` with
   pre-filled arguments; recipe validation with a mandatory approval point on every
   destructive step; the plan/apply pair as a declared contract; a chain id in a
   tamper-evident audit. Static rules AEOS100 to AEOS130 run in `axi ext lint`;
   behavioural rules AEOS140 to AEOS150 run through `axiom.infra.chain_conformance`
   against the real dispatch chokepoint.
5. **A recipe is a runtime-consumed kind, not a ninth core kind.** It has no body and
   nothing runs it, so it is not a skill; it is validated data, so it is not a
   prompt, though it is published as one. The core list stays closed at eight.
6. **The repository's ratchet is the same fact one level up.** The debt list stays;
   each built-in with no skill on it claims Level B in its manifest, a Level B
   extension may have none on it, and a test holds the two together.

## Options considered

- **Keep it a repository guard.** Rejected: it binds no one else and gives authors no
  statement of what is expected.
- **A generated sidecar file instead of manifest fields.** Rejected: a second artifact
  to keep in sync and to find, where the manifest is already the place a consumer
  looks and the generator already writes those tables.
- **Fold levels into Bronze, Silver and Gold.** Rejected: those grade packaging and
  trust; chain readiness is independent of them and an extension can be strong on one
  and absent on the other.
- **Make recipes a ninth core kind, or a kind of prompt.** Rejected, above.
- **Invent wire shapes.** Rejected: the survey found a public shape for most of it,
  and a client that has never heard of AEOS should still receive the safety hints.

## Consequences

- An extension can say what it has done and be held to it; an author moves up one
  level at a time with `axi ext lint --chain-level B` listing each finding and fix.
- `axi ext init` scaffolds Level B with a declared skill and a valid recipe stub.
- The manifest schema grows additively; the validator is the schema.
- Hints are advisory by the protocol's own terms. The enforcement is server-side and
  is what the behavioural rules test.
- Deferred: a recipe runner; per-harness client conformance of `_chain`; the
  Skills-extension binding; a lint mode that imports an external extension to compare
  its table with its code; consuming the Arazzo projection.
- Whether any of the AEOS delta is proposed to a public body is a separate decision,
  not taken here (section 4.11.9 of the spec).
