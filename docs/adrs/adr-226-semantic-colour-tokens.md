# ADR-226: One semantic colour-token system for every theme; a brand sets the accent only

**Status:** Proposed (2026-10-30). Stub recording a decision the owner settled during the Site view design; review before Accepted.
**Related:** PRD §19.3, §19.4; spec Addendum v12; ADR-123; PRD `docs/prds/prd-site-view.md`, spec `docs/specs/spec-site-view.md`, roadmap `docs/specs/spec-webapp-buildout-roadmap.md`

## Context

An audit across the designs found the same meaning in different colours (waiting drawn blue in one place and orange in another), the brand accent used as a status, and a dark palette whose ok and bad were nearly indistinguishable under deuteranopia.

## Decision

1. Every theme maps onto one token set: surface (`ground`, `panel`, `panel-2`, `well`, `rule`, `rule-strong`), text (`ink`, `ink-2`, `ink-3`), accent (`accent`, `accent-soft`, `accent-ink`), status (`ok`, `warn`, `bad`, `none`, `info`, each with `-tint`), alarm (`alarm-surface` derived from `bad`, `alarm-ink`), series (`series-1…n`), `held`.
2. **The accent is never a status. A brand sets only the accent tokens**; a product supplies the status palette. The accent is the one interactive colour. A brand's identity colour (its logo or wordmark colour) is identity only: never interactive, never a status, and never tinted into an accent (amended 2026-11-10).
3. Themes are chosen per surface: app (by person), console and wall (by device), public (by site), print, alarm.
4. A measured channel and its simulated counterpart share a series hue; the simulated one is dashed and labelled. Held data is neutral.
5. CI checks: status on panel ≥ 4.5:1; worst ΔE among ok, warn, bad ≥ 15 under normal vision, deuteranopia and protanopia; a brand accent within ΔE 20 of a status colour is refused.
6. Status always carries a glyph and a word; colour is never the only signal.
7. Tokens live in appkit's single token source (ADR-123); the served copy is generated.

## Consequences

- Final status hues wait on an owner decision (green vs blue "ok"); the token names do not.
- A brand that collides with a status colour is refused at configuration.
