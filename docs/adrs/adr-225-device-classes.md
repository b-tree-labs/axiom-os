# ADR-225: Three device classes, declared at enrolment and recorded in every signature

**Status:** Proposed (2026-10-30). Stub recording a decision the owner settled during the Site view design; review before Accepted.
**Related:** PRD §18; spec Addendum v11; ADR-223, ADR-227; PRD `docs/prds/prd-site-view.md`, spec `docs/specs/spec-site-view.md`, roadmap `docs/specs/spec-webapp-buildout-roadmap.md`

## Context

Fixed stations, tablets and phones were drawn as one responsive layout, but they differ in who may sign what, how presence is proved, and which theme fits the room. The design did not cover devices until v11.

## Decision

1. Device classes: **console** (a fixed station enrolled to a room), **tablet** (personal or kiosk; the mode is shown on every screen) and **phone** (never satisfies presence). A browser on a desk is the default class.
2. A `DevicePolicy` per class declares `may_sign`, `acknowledge_only`, presence (`enrolment`, `room_code`, `none`) and the theme default. A product supplies the act lists.
3. The class is declared at enrolment and **recorded in every signature** from the first one (the field lands in P0).
4. A signature on a class not allowed for the act is refused; an acknowledge-only act records an acknowledgement, never a signature.
5. A room-code proof lasts 60 s and is bound to the room. Offline drafts stay `pending` and are never rendered as signed.
6. Breakpoints: phone ≤ 900 px, tablet ≤ 1180 px; on phones the rail becomes a tab bar, the drawer a bottom sheet, tables card lists.

## Consequences

- Signatures carry their device class without a later migration.
- Phone signing stays off until the owner decides which acts allow it (open decision).
