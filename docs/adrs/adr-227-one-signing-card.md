# ADR-227: One signing card for every signature; the verb names the act

**Status:** Proposed (2026-10-30). Stub recording a decision the owner settled during the Site view design; review before Accepted.
**Related:** PRD §19.3, §19.5; ADR-225, ADR-228; PRD `docs/prds/prd-site-view.md`, spec `docs/specs/spec-site-view.md`, roadmap `docs/specs/spec-webapp-buildout-roadmap.md`

## Context

Four designs each drew their own approval or signature card, with different keys and different ways of showing what was being signed.

## Decision

1. One appkit signing card for every signature in every product: it shows **exactly what will be signed** (never folded), and its verb names the act (Sign, Sign receipt, Sign approval, Certify, Co-sign, Seal).
2. Every card offers **Sign · Hold · Ask** with the keys **A · H · ?**.
3. Signing writes an attested record: signer, act, content hash, device class, time. Hold records a reason. Ask opens a thread.
4. A product may add read-back text and voice; it may not fork the card.
5. An MCP or CLI call can draft a signature; only a person signs it.

## Consequences

- Publication approvals (the public site) and log signatures share one component, so the card is built first (P2.0).
- A lint refuses a second card.
