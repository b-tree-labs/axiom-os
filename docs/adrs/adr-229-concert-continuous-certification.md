# ADR-229: ConCert: certification is continuous, and the audit is inverted

**Status:** Proposed (2026-10-30). Stub recording a decision the owner settled during the Site view design; review before Accepted.
**Related:** PRD §15, §14.6; spec Addendum v6; ADR-227; PRD `docs/prds/prd-site-view.md`, spec `docs/specs/spec-site-view.md`, roadmap `docs/specs/spec-webapp-buildout-roadmap.md`

## Context

Audits are periodic, costly and backward-looking: evidence is assembled by hand at audit time. The platform already records signed entries, an audit chain and obligations continuously.

## Decision

1. **Controls** declare the evidence they need; evidence is **assembled continuously** from signed records, the audit chain and obligations.
2. The auditor's main screen is the **ConCert queue**: verdict and counts first, evidence one click.
3. **Certification posture** reads "certified through <date>" and lapses honestly when evidence stops.
4. Remote visual inspection first.
5. **External audit AIs** may work under a time-boxed, read-only grant scoped to the evidence; every query is audited; their findings are drafts a person signs.
6. Findings and seeded sampling live inside attest; corrective actions are loops.

## Consequences

- The auditor reads a live posture instead of a packet.
- Whether the standing review folds into one Audit workspace is an open owner decision.
