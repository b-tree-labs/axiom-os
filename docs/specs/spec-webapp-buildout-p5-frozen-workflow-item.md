# Spec: web app build-out, P5 a product's frozen workflow item

**Owner:** platform (web) • **Status:** Draft, hand-off • **Last updated:** 2026-10-30 • **Size:** L (mostly product work; the Axiom part is S)
**Roadmap:** [spec-webapp-buildout-roadmap.md](spec-webapp-buildout-roadmap.md) • **Product specifics:** the product's P5 hand-off holds the design itself

A product may bring a vetted, frozen workflow design and place it in the shell almost
wholesale. Axiom's job is to provide slots so the design does not have to change. This
spec lists only those slots; the product's hand-off lists the design.

## Entry criteria

- P3 gate (the workflow writes signed entries into a typed log) and P4 gate (kiosk
  devices, the live mode it cross-links to).
- The product's integrity audit of its frozen design shows no open conflict with the
  shell, or each conflict is decided by the owner.

## Design slice

| Source | Sections |
|---|---|
| Spec | §3.2 (product extension points) |
| PRD | §14.11–§14.12 (frozen-design conflicts resolved by shell shortcuts only); §19 preamble (a frozen design wins inside its area) and §19.1 (an item's own breadcrumb is allowed inside it) |
| Gallery | v12 canonical components (what the item may reuse) |

## Scope (Axiom slots)

1. **A workflow item slot** in the rail after Site (or where the product places it),
   with its own internal routes and its own crumb inside the right panel.
2. **Kiosk entry:** a kiosk device can open straight into one sub-page of the item.
3. **Cross-link chips:** the item's objects are chips elsewhere (Site, Log, Chart,
   Search, chat), and other objects are chips inside it; both open drawers.
4. **Needs you filtered beside the item's work**, using the same queue.
5. **Two status vocabularies:** the item may keep its own state words; the drawer shows
   the lifecycle state first and the product's word second (cohesion rule, product
   flag).
6. **Signing through the card:** the item's approvals use the one card (the product
   sets the verb).

**Out of scope.** Any change to the frozen design's lifecycle, objects, states, roles or
terms.

**Components reused.** Shell, drawer, Needs you, the card, DataTable, the map component.

**Flags.** The product declares its item's flag (default `soon` until P5 lands).

| ID | Acceptance | Trace |
|---|---|---|
| P5-A01 | A product registers a workflow item with internal routes and no shell edit | spec §3.2 |
| P5-A02 | A kiosk enrolled to a sub-page opens there and cannot leave it without an admin | P4-A08 |
| P5-A03 | The item's objects are searchable and open their drawers from Search and chat | R19.1 |
| P5-A04 | The item's approvals use the one card (lint) | P2-A01 |

## Deploy, verify, rollback

As in the product's hand-off. Rollback: the item's flag to `soon`.

**Estimate.** Axiom slots: S. The product's work: L.
