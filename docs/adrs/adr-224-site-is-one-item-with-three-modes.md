# ADR-224: Site is one rail item with three strictly separate modes

**Status:** Proposed (2026-10-30). Stub recording a decision the owner settled during the Site view design; review before Accepted. Owner decision D27 (2026-11-09): Control is off the rail; it is Site › Automate, as built, keeping its route `#/control` and its ⌘K name.
**Related:** PRD §14.1, §14.11; spec Addendum v5; ADR-211, ADR-223; PRD `docs/prds/prd-site-view.md`, spec `docs/specs/spec-site-view.md`, roadmap `docs/specs/spec-webapp-buildout-roadmap.md`

## Context

The design first had Site (structure) and Control (behaviour) as two rail items. Mockups blended them, and the hard case was the center of gravity: it is structure, yet operators want it live.

## Decision

1. **Site** is one rail item (a product may rename it) with three modes, each strictly one kind of content:
   - **Operate:** live; what is changing now.
   - **System:** configuration and architecture, with the views **Physical · Diagram · Data**.
   - **Automate:** agents, loops, knobs and dials.
2. The selection is kept across modes and views.
3. Persona presets choose the default mode.
4. This amends ADR-211: the Control item becomes the Operate and Automate modes. Proposed flag mapping (to confirm in review): `control` → `site.automate`, with the old `steer` alias kept.

## Consequences

- One gravity item; the rail is shorter.
- Mode declarations are linted so a mode cannot drift into showing another's content.
- A product's live-mode design (for example, a run timeline) lives in Operate.
