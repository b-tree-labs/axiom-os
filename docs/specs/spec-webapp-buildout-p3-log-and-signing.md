# Spec: web app build-out, P3 Log and signing

**Owner:** platform (web) • **Status:** Draft, hand-off • **Last updated:** 2026-10-30 • **Size:** L
**Roadmap:** [spec-webapp-buildout-roadmap.md](spec-webapp-buildout-roadmap.md)

## Entry criteria

- P2.0 (the signing card) merged; P1 gate passed.
- OD-1 (one Audit workspace) answered, or the Audit mode built as the shell for it with
  the standing review left as a separate tab until the decision.

## Design slice

| Source | Sections |
|---|---|
| PRD | §14.3 (Log is one rail item with typed logs, and an Audit mode); §14.5 (who sees what); §14.6 (compliance and the auditor function); §14.12 (waivers, findings, retention decisions); §19.1 (objects → drawer, acts → page); §19.2 (never hidden: the read-back, a hold); §19.3 (entry, record, event; the card's verbs; formats) |
| Spec | Addendum v5 (typed logs, Audit mode); Addendum v6 (the evidence a review uses) |
| Gallery | v5 Log and Audit screens; v12 "One signing experience, every book" (the generic card states) |

## Scope

1. **Log as one rail item** with typed logs: a log type is declared by an extension
   (fields, who may sign, co-sign rules, retention class). Axiom ships System and
   Change logs; a product declares its own.
2. **Entries.** An entry is a signed line (card); a correction is a supplement that
   references the original and is shown in the info colour; an acknowledgement is a
   separate act. Nothing is edited in place.
3. **Co-sign, hold, seal.** A log type may require a co-signature; a period (a shift, a
   day) may be sealed; sealed periods are immutable and anchored in the audit chain.
4. **Read-back.** Before a signature the card shows exactly what will be signed (never
   folded).
5. **Events draft entries.** An append-only event may draft an entry for a named person
   (Needs you › Held drafts); the person signs it (H7: never re-key).
6. **Audit mode.** A mode of Log: search, chain verification, period reconstruction,
   evidence export, findings (inside attest), seeded sampling. Time-boxed read-only
   auditor grants (MVP-07 attributes).
7. **Retention enforced.** Each log's class; site lengthens only; deletions attested;
   legal hold.
8. **Needs you** gains To sign and Co-sign sections.

**Out of scope.** ConCert and external audit AIs (P7); a product's log specifics (its
entry types, its run timeline: P4 product spec).

**Components reused.** The signing card (P2.0); DataTable; `LogbookPage`,
`LogbookView`, `EntryComposer`, `EntryForm`, `RecordThread`, `ProofBadge`; Axiom
`attest` (logbooks, obligations, verify), the audit chain.

**Flags.** `log=on` (was `soon`), `audit_mode=on` for auditor and admin roles only.

**Data and backend prerequisites.** Log types as declarations in the attest extension's
schema (ADR-052); the period seal anchored in the audit chain.

| ID | Acceptance | Trace |
|---|---|---|
| P3-A01 | A log type declared by an extension appears in Log with its fields, signers and retention, with no shell change | §14.3 |
| P3-A02 | A correction never alters the original; both show in the entry drawer | §19.3 |
| P3-A03 | A co-sign-required entry stays ⏳ until co-signed and shows in the co-signer's Needs you | §19.3 |
| P3-A04 | A sealed period refuses any new entry or supplement; the seal verifies in the chain | §14.3 |
| P3-A05 | An event-drafted entry is pre-filled and signed in one card | H7 |
| P3-A06 | Audit mode reconstructs a period (entries, changes, signatures) and exports a signed evidence package | §14.6 |
| P3-A07 | An auditor grant expires on time and is read-only throughout | §14.6 |
| P3-A08 | Every log act is on page, CLI (`axi attest …`), chat and MCP (`attest.*`) | ADR-072 |
| P3-A09 | A pending draft is never rendered as signed on any device | spec addendum v11 |

## Deploy and verify

1. Release Axiom and appkit; the product declares its log types in its own release.
2. Staging: enable `log=on` for the site variant first, then the bare variants.
3. Verify P3-A01…A09; run a sealed-period chain verification on the node.

**Rollback.** `log=soon` hides Log without touching records; records are append-only
and stay. Code rollback by the switch runner.

**Estimate.** L (packages: log types + entries + supplements; co-sign + seal +
retention; Audit mode).
