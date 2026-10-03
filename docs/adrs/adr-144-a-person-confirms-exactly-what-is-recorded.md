# ADR-144: A person confirms exactly what is recorded, and the presentation is kept

**Status:** Accepted (2026-09-30)
**Related:** [ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md),
[ADR-145](adr-145-voice-stays-on-site-and-a-spoken-answer-signs.md),
[ADR-146](adr-146-signing-from-the-browser.md),
[ADR-114](adr-114-mcp-authority-enforcement.md) (propose disposition),
[prd-receipts-surface.md](../prds/prd-receipts-surface.md) R20 (density grammar), R21 (interaction grammar),
[spec-attestation.md](../specs/spec-attestation.md) §Confirmation

## Context

Nearly every human act on the platform ends in the same step: something was
proposed, and a person agrees to it. The proposal can come from many places:

- an agent's held action;
- a case's proposed resolution;
- a steward declaration;
- a pre-filled log entry;
- a transcript of what someone just said;
- a value read from an instrument.

Each surface renders its own "are you sure", and none records what the person
actually saw.

Two failures follow. First, a signature proves someone clicked, not what they
agreed to. If the screen rendered a stale value, a rounded number or a
misheard word, the record cannot show it. Second, confirmation is tied to one
modality. A person whose eyes and hands are on a console needs the proposal
read back aloud and must be able to answer by voice. High-reliability operations already
require this pattern of the people in them: repeat-back, or three-way
communication.

## Decision

**Confirmation is a pluggable platform mechanism, and what a person confirms
is byte-for-byte what is recorded.**

- **Anything can be confirmable.** A subject kind registers a *presenter* that
  renders a proposal at the platform's R20 densities (strip, line, card, page) and
  as **speakable text**: units spoken in full, digits read singly where
  confusion is possible, identifiers spelled.
- **Presentation is derived from the record, never the reverse.** The
  presenter renders from the exact canonical content that will be signed. The
  presentation carries the `digest` of that content. A signature is accepted
  only for the digest that was presented.
- **Modalities are plugins:**
  - screen (appkit confirm card, built on the decision card that Steer and
    the docket already use);
  - voice (read back through the site's speech service and listen for the
    answer);
  - interactive notification (the existing `InteractiveChannel` variants
    for Slack, Teams and SMS, through the approval bridge);
  - kiosk;
  - CLI.
  A policy declared by the book, per meaning and entry type, says which are
  required, which are allowed, and whether several must agree.
- **The platform's one decision anatomy (R21), not a new one.** A
  presentation offers **Primary · Hold · Ask**, in that order and position:
  - **Primary** is *Sign*: sign exactly what is presented.
  - **Hold** keeps it as a draft, unsigned, visible in the signer's tray.
  - **Ask** opens the correction path, scoped to this proposal. The person
    edits, dictates a correction, or asks the chat a question, and a new
    presentation follows.
  Discarding a draft is not a fourth action on the card; it lives in the
  draft tray. The keyboard follows the docket keys (`a` sign, `h` hold, `?`
  ask). Spoken answers map onto the same three: "confirm" → Sign,
  "hold" → Hold, "correct …" → Ask.
- **Confirmation here is the gate class, not a habit.** R21's rule is undo
  over confirm, with confirmation only where the action is irreversible.
  Signing is irreversible (a signed record is never edited), which is why
  every signature is confirmed.
- **The presentation is evidence.** The attestation stores, per modality:
  - the rendered text (and speech text);
  - the presenter id and version;
  - the digest presented;
  - the response, including its transcript and audio checksum when spoken;
  - the response latency.
- **Timeouts never confirm.** A confirmation that is not answered stays a
  draft. Where a book has an obligation, the obligation's own rules apply.
- **Confirmation is not authentication.** A voice "confirm" counts only from
  the person authenticated at that console or session. Acts whose assurance
  level requires fresh authentication or a personal key complete that step
  as well (ADR-146).

**Observed fields are never offered.** A book may declare a field `observe`:
the person must observe it themselves and enter it.
- **Before signing.** Presenters, forms and every surface show no value for
  it: no pre-fill, no live reading beside the input, no suggestion. The
  read-back repeats only what the person entered or said.
- **After signing.** Machine sources may cross-check the field. A
  disagreement is recorded beside the signed value as a linked system fact
  and never replaces or edits it.

The rule exists because a record that says "I observed X" is worthless if
the screen supplied X.

Confirmation is used beyond attestations. An agent proposal confirmed through
this mechanism produces an attestation of meaning `approved`, which links the
two records.

## Options considered

- **Per-surface confirm dialogs** (status quo). Lost: they record nothing about
  what was shown, and there are as many designs as surfaces.
- **Record only the final signed content.** Lost: it cannot distinguish "the
  person saw 850 and signed 850" from "the screen showed 950 and the record
  says 850".
- **Voice as a separate feature of one book.** Lost: every book with hands-busy
  users needs it, and so do agent approvals away from a screen.

## Consequences

- `axiom.attest.confirm` defines the presenter protocol, the modality plugin
  protocol and the policy schema. appkit gains `ConfirmCard` and a voice
  read-back controller.
- The approval bridge's interactive notifications become a confirmation
  modality. The gate-is-the-record rule stands; the channel remains the
  doorbell.
- Presenters are testable pure functions from content to text. Speakable-text
  rules (numbers, units, identifiers) are shared and tested once.
