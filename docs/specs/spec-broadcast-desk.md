# Tech Spec: The Broadcast Desk (feature 7)

**Status:** Living. **PRD:** `prd-broadcast-desk.md`.

## 1. Shape

New builtin `dispatch/` (purpose-named):
- `sources.py` — period harvesters over existing stores: session
  receipts (F1), ledger actions, schedule fire log, fleet state
  transitions, drift verdicts. Each yields `ResultItem{kind, receipt
  ref, verified: bool, summary facts}` — facts only, no prose.
- `audiences.py` — audience registry: name, channel (connect fabric
  connection_ref), cadence, topics, review_mode (required |
  auto-allowed), composition template.
- `compose.py` — per-audience composition from one ResultSet:
  tracker-comment shape, chat-post shape, email shape, public-draft
  shape. Voice drafting via the existing press/voice machinery;
  UNPROVEN items render only with their flag (PRD R1).
- `desk.py` — the dispatch lifecycle: draft → injection edits →
  preflight → gate check → per-channel send → delivery receipts.
  Stuck-in-preflight staleness per PRD strawman #4.
- Gate: outbound content passes the serving gate
  (classification/site-rule screens) before any send (PRD R5).

## 2. Surfaces

- **Desk UI** (Receipts surface page): the one screen — period result
  list (verified badges), per-audience drafts side by side, inline
  injection editing, preflight approve per audience, dispatch log
  with receipts. appkit components: `DispatchDesk`, `AudienceColumn`,
  `InjectionEditor` (builds on InlineEditable).
- CLI thin verbs: `axi dispatch draft|show|send` (ADR-056).
- Today (F0 R11) links the pending desk state ("2 dispatches in
  preflight").

## 3. Reuse ledger

| Reused | For |
|---|---|
| signals synthesizer/changelog machinery | period narrative scaffolding |
| press voice drafting | R3 founder-voice drafts |
| HERALD + connect channels (Teams/email/webhook + tracker API) | delivery |
| serving/classification gate | R5 outbound screen |
| RACI/ApprovalGate | preflight approvals |
| action ledger | dispatch receipts + sources |
| F1 session receipts / fleet transitions / drift verdicts | content |
| appkit InlineEditable + F0 shell | the desk UI |

New: harvesters, audience registry, composition templates, desk
lifecycle, three appkit components.

## 4. Cross-platform

Server-side feature; no OS surface beyond the existing shells (glyph
badge for preflight-waiting).

## 5. Sequencing

After F1 (session receipts are the marquee source); before or beside
F2's P3 (spend lines enrich dispatches but aren't required).
Phases: P1 harvesters + one audience (the program tracker comment —
replacing this week's manual ritual) end-to-end with review; P2 the
desk UI + injection + multi-audience; P3 public-draft audience wired
to the campaign flow + delivery-receipt staleness.
