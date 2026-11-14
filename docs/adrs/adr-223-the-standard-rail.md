# ADR-223: Every app on appkit uses one standard rail and a configurable landing

**Status:** Proposed (2026-10-30). Stub recording a decision the owner settled during the Site view design; review before Accepted. Amended 2026-11-10 to the master design's decision D1 and the owner: Model and Chart show by user type, Team is universal (on by default, may be switched off), and Search is a row under the wordmark plus ⌘K, not a rail item.
**Related:** PRD §12.1, §12.2, §12.3, §13.1, §14.1; ADR-211, ADR-224; PRD `docs/prds/prd-site-view.md`, spec `docs/specs/spec-site-view.md`, roadmap `docs/specs/spec-webapp-buildout-roadmap.md`

## Context

The Site view design went through several rails, including one grouped under section headers that the owner rejected as hard to follow. Products built on appkit each drew their own order, so a person moving between products relearned the navigation.

## Decision

1. One rail, top to bottom, with no section headers: **Chat · Site · [Model] · [Chart] · Log · Library · Team**, then **History** at the bottom. Model and Chart are shown by user type: the product decides from a person's user type or role whether they appear. Team is universal: on every rail by default, and a person or site may switch it off. A hidden item stays reachable through Search and ⌘K, and through a More item at the bottom of the rail that appears only while something is hidden. More lists the hidden items, each with Pin to rail. Settings is where the rail is changed (D1, D11). **Search** is not a rail item: a full-width row under the wordmark plus ⌘K opens one search modal (on a phone, a magnifier in the top bar).
2. A product may **rename** Site and Model, **insert** its own items after Site, and **hide** items by persona. It never renames Library and never reorders the standard items.
3. Persona presets show or hide items and set the landing; a person may override both.
4. The landing page is configurable and **defaults to full-page chat**. The center of gravity is reached from its rail noun, so there is no separate Home item.
5. Audit is a mode of Log, not a rail item.

## Consequences

- One navigation model for every product; the bottom tab bar on phones takes the role's first four items plus More (ADR-225).
- A persona whose most frequent task is reconstruction takes 2 clicks instead of 1 (PRD §14.7, accepted by decision).
- ADR-211's separate Control item is superseded by ADR-224.
