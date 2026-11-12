# Spec: web app build-out, P4 Site view, live mode, devices and walls

**Owner:** platform (web) • **Status:** Draft, hand-off • **Last updated:** 2026-10-30 • **Size:** XL
**Roadmap:** [spec-webapp-buildout-roadmap.md](spec-webapp-buildout-roadmap.md) • **Product specifics:** the product's P4 hand-off (its live mode and logs)

## Entry criteria

- P3 gate passed (typed logs, the card's co-sign and seal).
- OD-3 (phone signatures) and OD-7 (station theme override) answered, or the defaults
  in spec addendum v11 taken (phone acknowledges only; no override on a station).

## Design slice

| Source | Sections |
|---|---|
| PRD | §5.1–§5.10 (systems, loops, measured and predicted channels, lineage, improvement loop, view modes, scope, ambient entry, kinds of site, specialization); §11 (archetypes, disclosure, gravity, layout); §12.5 (a glance before the table); §14.1–§14.2, §14.4 (modes, drill-downs, links); §14.8–§14.9 (empty states, scale); §16 (alive, chart slots, wall displays, movement, feel); §18 (devices); §19.5 (one map component) |
| Spec | §3–§8 (base interface, loops, skeleton and catalog, coordination, scope, lineage, layout and physical representations); Addenda v5 (modes), v7 (chart slots, walls), v11 (device classes) |
| Gallery | v1–v4 (the Site view, physical, systems, data, lineage, loops, scope, farm and factory examples), v5 (Site modes, drill-downs, journeys), v7 (alive, chart slots, as-built, walls), v11 device screens (phone and tablet), v12 canonical components |

## Scope

1. **Site, one item with three modes** (ADR-224): **Operate** (live: what is changing
   now), **System** (configuration and architecture, with Physical · Diagram · Data
   views), **Automate** (agents, loops, knobs). Each mode is strictly one thing.
2. **The system catalog and base interface** (spec §3–§4): extensions contribute
   systems (health, inventory, relations, tile, drill-in) on a control-structure
   skeleton; loops are first-class (trigger → act → observe → evaluate).
3. **Coordination** (spec §5): relations, the "why is the site degraded?" roll-up, one
   evaluation with many readers.
4. **Lineage** (spec §7): the path of any value, measured on its two channels (live and
   batch) and predicted, with cross-channel reconciliation as a health signal.
5. **One map component** (R19.5): declared (System), live (Operate), history (Log).
6. **Chart slots** (PRD §16.3): any view adds a chart from the catalog by reference.
7. **Wall displays** (PRD §16.4): any status, chart, schedule or Site view full-screen;
   an editor composes walls; normal muted, colour for abnormal (ISA-101 style); measured
   vs simulated labelled. Built on the P0 Expand behaviour (`Expandable` /
   `DisplayMode`): display mode (a whole page, or a group made into a display) drops
   the app chrome, refreshes live, uses large type, and shows a stale banner so stale
   data never looks live; it has a burn-in-safe layout. A **shareable display URL**
   serves a kiosk screen whose identity is an actor of kind "display" (read-only,
   site-scoped, its own credential, revocable; ADR-225 device model). Optional
   **auto-cycle** between saved displays. Public displays follow the public-site rules
   (published data, the 24-hour delay); internal walls use internal data.
8. **Devices** (ADR-225, PRD §18): console, tablet (personal or kiosk; the mode shown on
   every screen), phone (never satisfies presence); enrolment; the device policy
   (`may_sign`, `acknowledge_only`, presence by enrolment or room code, theme default);
   offline drafts stay pending; the bottom tab bar with a product's pinned item.
9. **Persona presets** set default modes and landings (PRD §14.11).

**Out of scope.** A product's live-mode content (its run timeline, its checks: the
product's P4 hand-off); Model's improvement loop UI (P6); camera feeds beyond a
placeholder slot.

**Components reused.** NodeApp shell, `DetailDrawer`, DataTable, charts and figure lane,
`AlarmBand`, `CaseCard`, `ObligationCountdown`, `LiveIndicator`, `useLiveStream`,
`ModeChip`, `StatTile`, `VerdictStrip`; Axiom `sites`, `signals`, `data_platform`
lineage, `schedule`, `agents`.

**Flags.** `site=on` (was `soon`); `site.automate=soon`; `walls=on` for editors;
`devices.tablet_kiosk=on`; `devices.phone_sign=off`.

**Data and backend prerequisites.** `axiom/site/systems/contract.py` and
`axiom/site/loops/contract.py` (spec §3, §3.1); the bound-app forwarding prerequisite
(spec §12.1); the device enrolment store.

| ID | Acceptance | Trace |
|---|---|---|
| P4-A01 | An extension's system appears in the catalog and the roll-up with no shell change | spec §3–§4 |
| P4-A02 | "Why degraded?" names the contributing systems and loops in ≤ 2 clicks | §5, H1 |
| P4-A03 | Any value's lineage shows its channel(s), hops and freshness; reconciliation disagreement raises ? | spec §7.3 |
| P4-A04 | The selection carries across Physical, Diagram and Data views and across rail items | §5.6, H5 |
| P4-A05 | Operate, System and Automate each show only their kind of content (lint on mode declarations) | ADR-224 |
| P4-A06 | A chart slot references a catalog id; editing the chart updates every slot | §16.3 |
| P4-A07 | A wall runs 24 h without a memory leak and recovers after a network drop with an honest stale state | §16.4 |
| P4-A08 | A signature on a device class outside `may_sign` is refused; `acknowledge_only` records an acknowledgement | spec addendum v11 |
| P4-A09 | A room-code proof expires after 60 s and binds to the room | spec addendum v11 |
| P4-A10 | At ≤ 900 px the shell follows the phone rules; a product's pinned item is in the tab bar for its roles | §18.2 |
| P4-A11 | Scale: 50 sites, 10 000 channels, 100 loops render the catalog in < 2 s (p95) | §14.9 |
| P4-A12 | Empty and first-run states name what fills them and offer one act | §14.8, H4 |

## Deploy and verify

1. Release Axiom and appkit; the product releases its systems and live mode.
2. Staging: `site=on` for the site variant; enroll one console, one tablet, one phone.
3. Verify P4-A01…A12; run a wall on the staging node for 24 h.

**Rollback.** `site=soon`; device enrolments persist and are harmless while off. Code
by the switch runner.

**Estimate.** XL (packages: systems + roll-up + lineage; modes + map + chart slots;
walls; devices).
