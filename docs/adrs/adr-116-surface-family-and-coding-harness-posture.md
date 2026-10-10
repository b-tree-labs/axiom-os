# ADR-116: Surfaces Are One Extension Family; Coding Harnesses Are Partners, Not Products

**Status:** Draft (2026-09-18)
**Deciders:** Benjamin Booth
**Related:** [prd-embodied-surfaces](../prds/prd-embodied-surfaces.md), [spec-embodied-surfaces](../specs/spec-embodied-surfaces.md), ADR-031 (extension self-containment), ADR-032 (standards positioning), ADR-039 (scientific displays), ADR-051 (cross-provider context), ADR-055 (governance fabric), ADR-067 (HERALD gateway inbound), spec-aeos-0.1.md §4–§5, the node-foundations subsystem contract §7 (actuators)

## Context

Axiom reaches users today through a CLI and a chat surface, with a web
application kit building on the same primitives in its own repository. The
product direction adds more surfaces: mobile, voice/avatar presence, and
physical actuation (robotic fleets at industrial sites). Meanwhile the
2026-09-18 competitive analysis shows the coding-harness market (IDE and
terminal agents) fully commoditized on UX, with distribution controlled by a
handful of vendors — and every one of those vendors now reads the open
context, protocol, and skill standards we already publish to.

Two forks in the road, decided together because they are the same posture:
what Axiom builds as user surfaces, and what it deliberately does not.

## Decision

1. **Every user surface is an AEOS extension in one family, on one
   governance spine.** CLI, web, chat, mobile, voice/avatar, and actuation
   surfaces are extensions composed from existing capability kinds (§4) —
   no surface gets private access to the runtime. A surface qualifies for
   the family by conformance (spec-embodied-surfaces §5): it binds actions
   to an identified principal, routes effects through the policy engine and
   approval gates, and emits receipts as memory fragments. A product built
   on Axiom inherits its UI from the family the way it inherits governance.
2. **Axiom builds no coding-IDE or coding-terminal surface.** External
   coding harnesses — Claude Code, Codex CLI, OpenCode, Cursor, Zed,
   JetBrains, and peers — are first-class *partners* served aggressively
   through the standards seams we already maintain: canonical `AGENTS.md`
   with per-harness context generation (ADR-051), the composed MCP server,
   ACP compatibility, SKILL.md skills, and cross-tool memory adapters
   (OpenCode's transcript adapter sits beside Claude Code's and Codex's).
   Where a harness needs a new seam, we build the seam, not the harness.

## Options considered

- **Ship an IDE plugin / terminal coding agent.** Rejected: the UX frontier
  there commoditized in 2026, distribution belongs to incumbents, and a
  two-archetype product splits a small team's identity. The reach argument
  is served better by the standards seams those harnesses already read.
- **Treat surfaces as per-product bespoke apps.** Rejected: it duplicates
  governance wiring per product and guarantees drift — the exact failure
  the extension system exists to prevent.
- **A privileged first-party UI layer inside the core.** Rejected: violates
  ADR-031 self-containment and would make every future surface a core
  change instead of an extension.

## Consequences

- The web application kit's patterns generalize into surface-kit
  conformance requirements rather than remaining app-specific precedent.
- Voice/avatar and actuation surfaces get PRD + spec treatment now
  (embodied surfaces), mobile follows the same template when scheduled.
- Coding-harness support becomes a tracked product surface with its own
  bar: context files current, MCP conformant to the live spec revision,
  ACP client compatibility verified, memory adapters maintained.
- "Axiom is not a coding tool" becomes a positioning sentence we can spend
  against every "yet another harness" comparison.
