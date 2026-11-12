# Spec: web app build-out, P7 assurance and reach

**Owner:** platform (web) • **Status:** Draft, hand-off • **Last updated:** 2026-10-30 • **Size:** XL
**Roadmap:** [spec-webapp-buildout-roadmap.md](spec-webapp-buildout-roadmap.md)

## Entry criteria

- P3 gate (Audit mode); P4 gate (devices); P2 gate (public canvas) for partner canvases.
- OD-1 (one Audit workspace) and OD-3 (phone signatures) decided.

## Design slice

| Source | Sections |
|---|---|
| PRD | §15.1–§15.8 (ConCert: controls and continuous evidence, the auditor's queue, remote visual inspection, posture, the loop, external audit AIs, who); §14.6 (compliance and the auditor); §17.15 (public canvas per site, network page); §18.3–§18.5 (persona by device, offline, identity) |
| Spec | Addendum v6 (ConCert contracts); Addendum v11 (device identity) |
| Gallery | v6 (ConCert); v10 (public canvases, network); v11 phone screens |

## Scope

1. **ConCert, the inverted audit** (ADR-229): controls with continuous evidence
   assembly; the auditor's main screen is the ConCert queue (verdict and counts first,
   evidence one click); remote visual inspection; certification posture
   ("certified through"); the ConCert loop.
2. **External audit AIs:** an auditor may bring an AI under a time-boxed, read-only
   grant scoped to the evidence; every query is audited; its findings are drafts a
   human signs.
3. **The Audit workspace at full depth** inside Log › Audit (shape per OD-1).
4. **Voice:** voice entry and, where the product allows, voice signing (read-back
   spoken, then confirmed); off by default; never the only path.
5. **Phone:** acknowledge everywhere; sign only for acts the product lists and the
   policy allows (OD-3).
6. **Partner public canvases and the network page** for every site beyond the first,
   each configured in its own site repo, through the same publishing loop.

**Out of scope.** Regulatory submission formats (product work).

**Components reused.** Audit mode (P3), the card (P2.0), device policy (P4), public
canvas and the publishing loop (P2), chat (voice mode), notifications.

**Flags.** `concert=soon` → `on` per site; `audit_ai=off` (per grant);
`voice=off`; `devices.phone_sign=off` unless OD-3 says otherwise; `network_page=on`.

| ID | Acceptance | Trace |
|---|---|---|
| P7-A01 | A control's evidence assembles continuously; the queue shows its verdict without a manual pull | §15.1 |
| P7-A02 | Certification posture reads "certified through <date>" and lapses honestly | §15.4 |
| P7-A03 | An external audit AI's grant is time-boxed, read-only and scoped; every query is in the audit chain | §15.6 |
| P7-A04 | An AI finding becomes a signed finding only after a human signs it on the card | §15.6, P2-A01 |
| P7-A05 | Voice signing reads back the content and requires confirmation; it is refused where the product disallows it | §18, OD-3 |
| P7-A06 | A new site's canvas goes live from configuration only, through the loop | §17.15 |

## Deploy and verify

Release; staging with one control set and one auditor grant; one partner canvas on a
staging edge path.

**Rollback.** Flags off; grants revoked; canvases withdrawn through the loop.

**Estimate.** XL (packages: ConCert core; audit AIs; voice and phone; partner canvases).
