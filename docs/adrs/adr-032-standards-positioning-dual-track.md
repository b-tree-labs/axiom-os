# ADR-032: Standards Positioning — Dual-Track AEOS Strategy

**Status:** Accepted
**Date:** 2026-04-21
**Deciders:** Benjamin Booth
**Related:** ADR-031 (extension self-containment); spec-aeos-0.1.md

---

## Context

The agentic AI harness ecosystem has gone from "no interoperability standards" (mid-2025) to at least eight competing and partially-overlapping efforts in twelve months. The Linux Foundation's Agentic AI Foundation (AAIF), launched December 2025, has become the center of gravity, with Model Context Protocol (MCP), goose, and AGENTS.md as founding projects, and AGNTCY's Open Agentic Schema Framework (OASF) under its umbrella. Recent packaging-format entrants include Oracle's Open Agent Spec, Open Agent Format (openagentformat.com), Agent Packaging Standard (agentpackaging.org), `.agent` (agentpk.io), and Letta's `.af`.

Axiom's extension system predates and exceeds any of these in scope — it covers agent, tool, cmd, service, adapter, skill, and hook capabilities in one manifest with signed releases, federation-native features, and standardized test base classes. Publishing this as a standalone ninth competing standard would almost certainly fail; every successful standard in this space has foundation backing.

However, Axiom's leap-ahead features — federation-native distribution, behavioral attestation, trust-profile-governed installation, validated-classification quarantine/recovery — are not covered by any existing standard and differentiate Axiom commercially. Publishing them openly early would commoditize Axiom's moat; hiding them entirely would leave Axiom's standards position invisible.

The original attempt at a similar standard, Substrate's "The Agent Protocol" (TAP), never achieved public publication and its intent has migrated into Axiom. That experience informs this decision.

## Decision

We adopt a **dual-track standards positioning**:

### Public Track: Contribute to AAIF-adjacent standards

- Adopt MCP as the tool invocation protocol inside Axiom extensions
- Adopt A2A for agent-to-agent communication
- Adopt SKILL.md (agentskills.io specification) format for skill capabilities
- Adopt AGENTS.md for repository-level coding-agent guidance
- Use MCPB format conventions for MCP-server-only extensions (compatibility layer)
- Use OASF schema patterns for capability metadata where compatible
- Submit extension-format contributions to AAIF projects (OASF, MCPB) as appropriate, without exposing Axiom-specific leap-aheads
- Be a visible, constructive contributor in LF working groups

### Private Track: AEOS as internal delta

- Maintain Agent Extension Open Standard (AEOS) as an internal specification capturing everything the public standards do not
- AEOS includes the full seven-capability-kind taxonomy, compound extension layout, federation-native features, behavioral attestation, trust-profile governance, quarantine/recovery ceremony, and validated-classification pattern for extensions
- AEOS conformance is required of all Axiom, Keplo, Vyzier, and consumer-layer extensions
- AEOS documentation is written to be publishable, but kept in the Axiom repository (under `docs/specs/`) rather than released as a standalone project
- The AEOS spec is versioned (AEOS 0.1, 0.2, ...) to preserve optionality for later publication

### Transition Triggers

AEOS parts transition from private to public under these conditions:

- **Donate to AAIF/OASF** when: (a) a specific feature has clear ecosystem demand, (b) donating does not reveal proprietary leap-ahead logic, (c) donation strengthens Axiom's position as a foundational contributor rather than weakening it commercially. Example: the seven-capability-kind taxonomy might be donated to OASF as a schema extension.
- **Release AEOS publicly as an independent standard** when: (a) an AAIF path is closed or too slow, (b) there is clear external demand (non-Axiom adopters asking for it), (c) Axiom has enough ecosystem presence that publishing is credible. Target: earliest late 2027, pending federation consortium growth.
- **Continue to morph AEOS privately** when: (a) AAIF absorbs the public-track pieces adequately, (b) AEOS deltas remain Axiom-specific differentiators, (c) the standards landscape is still too unstable for a new public standard.

### Compatibility Commitment

Every AEOS-conformant extension is simultaneously:
- MCP-compatible for its MCP-protocol-exposed tools
- A2A-compatible for its agent capabilities
- SKILL.md-compatible for its skill directories
- AGENTS.md-compatible at the repository level
- MCPB-installable for its MCP-server subsets

Extensions that only use the public-standard subset of AEOS can be loaded by any harness implementing those standards. Extensions using AEOS leap-ahead features require AEOS-conformant runtimes (currently Axiom).

## Consequences

### Positive

- **Preserves optionality.** AEOS can become a published standard, a donated set of contributions, a de facto convention, or a proprietary differentiator — we decide based on ecosystem evolution, not upfront commitment.
- **Avoids 9th-standard failure.** Not publishing prematurely sidesteps the adoption risk that killed most competing formats.
- **Protects leap-ahead moat.** Federation, attestation, quarantine/recovery, and governance features remain differentiated while public-track contributions earn standards-body credibility.
- **AAIF participation builds reputation.** Contributing to OASF and MCPB establishes B-Tree Labs and UT as credible contributors, which matters when we later propose AEOS donations.
- **Compatibility is additive, not conflicting.** Every extension works with existing standards AND gains AEOS leap-aheads; no "Axiom vs MCP" false-choice positioning.
- **Learns from TAP.** Substrate's TAP failed because it was published prematurely without foundation backing. Dual-track avoids that failure mode.

### Negative

- **Invisibility cost.** Keeping AEOS private means the ecosystem doesn't know it exists, so contributors can't find or adopt it. Partially mitigated by the spec being committed to the public Axiom repo; full mitigation requires eventual public release.
- **Bifurcation risk.** Axiom authors write against AEOS; external harnesses write against public standards. Extensions using AEOS leap-aheads won't run on non-Axiom harnesses. This is intentional (it's what makes AEOS-leap-ahead a moat) but limits cross-harness parity for those features.
- **Standards-body timing risk.** If AAIF moves faster than expected on packaging formats, Axiom might end up conforming to the new standard rather than contributing to it. Mitigate by active participation.
- **Documentation discipline burden.** Maintaining AEOS as a publishable-quality spec alongside private implementation requires sustained investment.

### Neutral

- **"Internal draft" framing preserves future options.** AEOS documents are marked "Internal draft — intended for eventual public release pending strategic decisions." This is neither hidden nor prematurely published.

## Implementation

1. **Publish spec-aeos-0.1.md** in `axiom/docs/specs/` as the authoritative internal specification. Mark status as "Draft — Internal."
2. **Integrate AEOS conformance into CI.** Every extension in Axiom, Keplo, Vyzier, and consumer layers must pass `axi ext lint` which enforces AEOS.
3. **Commit to AAIF observation.** Monitor MCP, OASF, MCPB, A2A, SKILL.md evolution. Contribute where strategically sensible.
4. **Maintain compatibility matrix.** Document in AEOS spec what subset of AEOS maps to each public standard, updated as standards evolve.
5. **Quarterly strategic review.** Every quarter, re-evaluate donation triggers. Decision: hold, donate N features, release publicly.
6. **Never advertise AEOS externally before a strategic decision to publish.** AEOS is visible to Axiom contributors through the repository; it is not marketed, promoted, or pitched to external users, investors, or partners except in contexts where standards conformance is explicitly discussed.

## Alternatives Considered

### Alternative 1: Publish AEOS immediately as standalone standard
Rejected. Would be the ninth competing format in a crowded early-stage landscape. Prior evidence (Substrate TAP) shows standalone standards without foundation backing fail to adopt.

### Alternative 2: Abandon standards ambition; use only AAIF-approved primitives
Rejected. Existing public standards do not cover Axiom's leap-ahead features. Adopting only the public subset forfeits the federation, attestation, and governance differentiators that are Axiom's commercial moat.

### Alternative 3: Donate AEOS immediately to AAIF as full contribution
Rejected. Donating prematurely exposes proprietary differentiators. Better to donate specific non-differentiated pieces (taxonomy, layout) while keeping federation and governance internal.

### Alternative 4: Publish AEOS as public de facto standard under B-Tree Labs stewardship without foundation backing
Rejected. Same failure mode as standalone publication. Without AAIF or CNCF backing, adoption is unlikely.

## References

- spec-aeos-0.1.md — the AEOS specification itself
- ADR-031 — extension self-containment (enforces layout compatible with AEOS)
- brand-product-strategy.md — portfolio context
- [Linux Foundation AAIF launch (Dec 2025)](https://www.linuxfoundation.org/press/linux-foundation-announces-the-formation-of-the-agentic-ai-foundation)
- [AGNTCY / OASF](https://docs.agntcy.org/oasf/)
- [MCP Bundle (MCPB)](https://github.com/modelcontextprotocol/mcpb)
- [Agent Skills specification](https://agentskills.io/specification)
