# ADR-228: The public site is static, separate from the node, and fed by attested published snapshots

**Status:** Proposed (2026-10-30). Stub recording a decision the owner settled during the Site view design; review before Accepted.
**Related:** PRD §17; spec Addendum (public site and intake), Addendum v9; ADR-227; PRD `docs/prds/prd-site-view.md`, spec `docs/specs/spec-site-view.md`, roadmap `docs/specs/spec-webapp-buildout-roadmap.md`

## Context

A site wants a public face that attracts funding, students and partners, and an intake path. Guest access to the node's chat was closed for risk, and the node must not be exposed to the public internet.

## Decision

1. The public site is **static HTML on a public edge**, never served by the node, with no runtime calls back.
2. Charts come from **published snapshots** (frozen spec + data) after an approval on the signing card that checks labels and quality. Held or above-public data is refused.
3. A governed publishing loop: draft → approval → build → publish → withdraw, all attested.
4. **One narrow intake door:** a write-only, rate-limited endpoint feeding a staff queue; requests become invitations.
5. A delayed live view runs under a standing approval with a site-set minimum delay; the day replay is rebuilt nightly.
6. Insiders sign in through the app's SSO; the static site never handles credentials.
7. One codebase: a public canvas per site, configured in the site repo, with a site-scoped communications-editor role; a subtle, configurable "powered by" credit.
8. Optional public questions (an assistant) are off by default.

## Consequences

- Nothing public can reach the node; a compromise of the edge exposes only published data.
- Publication freshness is bounded by the standing approval's schedule (open decision on cadence).
