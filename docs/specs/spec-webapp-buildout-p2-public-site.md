# Spec: web app build-out, P2 public site (with P2.0, the signing card)

**Owner:** platform (web) • **Status:** Draft, hand-off • **Last updated:** 2026-10-30 • **Size:** S (P2.0) + L (P2)
**Roadmap:** [spec-webapp-buildout-roadmap.md](spec-webapp-buildout-roadmap.md) • **Product specifics:** the product's P2 hand-off (product repo)

## Entry criteria

- P1 gate passed: Chart's catalog with stable ids (MVP-03-A2), quality marks
  (MVP-18), labels (MVP-22), invitations (MVP-14), the audit trail (MVP-13).
- For P2 proper: P2.0 merged.
- OD-5 (publications as files in a folder in the as-built Library; no new screen) and OD-8 (freshness cadence) answered, or the
  recommended defaults taken and recorded in the PR.

## Design slice

| Source | Sections |
|---|---|
| PRD | §17.1–§17.16 (static and separate, snapshots, intake, publishing loop, look, delayed live view, replay, audiences, credit, optional public questions, parity matrix, reuse, insiders, public canvas); §19.3 (the signing card); §19.4 (public theme) |
| Spec | Addendum "public site and intake contracts"; Addendum v9 (delayed live view, replay, credit); Addendum v12 (`ThemeChoice.surface = public`) |
| Gallery | v8 (marketing and intake), v9 (delayed twin, funding, students, parity pairs), v10 (insider sign-in, public canvas, network), v12 "Publication approval uses the card" |

## P2.0 The signing card

**Scope.** One appkit component and one Axiom act: the card shows exactly what will be
signed, its verb names the act (Sign, Sign approval, Certify, Co-sign, Seal, Sign
receipt), and offers Sign · Hold · Ask with keys A · H · ?. Signing writes an attested
record (signer, act, content hash, device class from P0, time); Hold records a hold
with a reason; Ask opens a thread to the requester. A product adds read-back text and
voice later, never a fork (ADR-227).

**Reuse.** Axiom `attest`; appkit `ConfirmCard`, `DecisionActions`, `signing.ts`,
`EvidenceRow`.

| ID | Acceptance | Trace |
|---|---|---|
| P2-A01 | One card component is used for every signature in the app (lint: no second) | R19.5 |
| P2-A02 | The record's content hash matches what the card displayed (snapshot test) | R19.2 never-hidden |
| P2-A03 | A, H and ? work from the keyboard on every card | R19.3 |
| P2-A04 | The record carries the device class | P0-DEV-A1 |
| P2-A05 | Sign is on page, CLI (`axi attest sign`), chat (confirm in chat, then the card) and MCP (`attest.sign`, which returns a pending draft a person signs) | ADR-072 |

## P2 The public site

**Scope.**
1. **Static, separate.** The public site is static HTML built from the same appkit
   components (`ThemeChoice.surface = public`), deployed to a public edge, never served
   by the node, with no calls back (PRD §17.1).
2. **Published snapshots.** A chart from Chart's catalog is published as a snapshot
   (data + spec, frozen) after an approval on the signing card that checks labels and
   quality (§17.2, §17.4). A held or refused span is never published (✕).
3. **The publishing loop.** Draft → approval (card) → build → publish → withdraw, all
   attested, run by the publishing extension (PRESS) (§17.4).
4. **Intake.** One write-only, rate-limited endpoint; requests land in a staff queue
   (Needs you › Due) and become invitations (MVP-14) (§17.3).
5. **Delayed live view and replay.** A standing publication approval publishes data at
   least N hours old (the site sets N) on a schedule, with the quality gate; the day
   replay is rebuilt nightly (§17.7, §17.8).
6. **Audiences and credit.** Funding, recruiting, services paths; a subtle
   configurable "powered by" credit, program and team credited first (§17.9, §17.10).
7. **Insiders.** A quiet "Sign in" goes to the app's SSO (the static site never handles
   credentials); "Open in the app" deep links (§17.14).
8. **A public canvas per site.** The section library and a per-site canvas
   configuration in the site repo; a site-scoped communications-editor role publishes
   through the same loop; sections appear as a site's data matures; a network page
   (§17.15).
9. **Optional public questions:** off by default; not built in P2 (§17.11).

**Out of scope.** Partner canvases for sites beyond the first (P7); a public assistant.

**Components reused.** appkit charts and figure lane (static render), tokens, brand
layer, DataTable (static), the signing card; Axiom `publishing`, `charts` catalog,
`notifications`, gate invitations.

**Flags.** `public_site=on` (node: publishing enabled), `public_questions=off`,
`delayed_live=soon` until the standing approval is signed.

**Data and backend prerequisites.** Snapshot export (spec + data, labels, quality) from
the catalog; a public-edge deploy target (static hosting) configured in the site repo;
the intake endpoint behind rate limits on the edge.

| ID | Acceptance | Trace |
|---|---|---|
| P2-A10 | The public build makes no request to the node at runtime (network test) | §17.1 |
| P2-A11 | A snapshot publishes only after an approval on the card; withdrawal removes it at the next build | §17.4 |
| P2-A12 | A span with a held mark or an export-control label above public is refused with a reason | §17.2, MVP-18, MVP-22 |
| P2-A13 | The delayed view never shows data newer than the site's delay (clock test) | §17.7 |
| P2-A14 | An intake request appears in staff Needs you within one minute and converts to an invitation in one click | §17.3, MVP-14 |
| P2-A15 | The intake endpoint refuses above its rate limit and accepts no reads | §17.3 |
| P2-A16 | "Sign in" lands on the app's SSO; the static site sets no cookies | §17.14 |
| P2-A17 | A canvas section with no published data is hidden, not empty | §17.15 |
| P2-A18 | Lighthouse accessibility ≥ 95 and no horizontal scroll at 390 px | R19.4, MVP-20 |
| P2-A19 | The product's parity matrix rows are each covered, improved or explicitly not carried | §17.12 |

## Deploy and verify

1. Release Axiom, appkit, the product; the site repo adds its canvas configuration.
2. Build the public site in CI; publish to a staging edge path first.
3. Verify P2-A10…A19 on the staging edge; run the product's parity matrix check.
4. Switch the public DNS or path to the new build (the site's step).

**Rollback.** The edge keeps the previous build; switching back is one step. Withdraw
any snapshot through the loop (attested).

**Estimate.** P2.0: S. P2: L (three packages: snapshot + loop; site build + canvas;
intake + delayed view).
