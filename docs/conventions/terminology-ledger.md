# Terminology Ledger

**Status:** Living. Founder direction 2026-09-24: minimize lingo;
force-introduce a term only where the introduction pays; standardize
on well-known proof-theory vocabulary where it is BOTH standard and
plain. Enforcement: the can-fail check below.

## The three tiers

Every surface-visible term is classified. A term not in this ledger
that appears on an operator-facing surface is a defect.

**PLAIN** (no introduction needed; the word means what it says):
claim · evidence · verified · unproven · stale · contradicted ·
counterexample · case · window · hold · approve · focus · owner ·
blast radius (borderline; vivid enough to pass) · trust change
(surface form of trust delta) · Ask · Auto · on the record.

**INTRODUCED-ONCE** (deliberately forced; the one-sentence
introduction is part of the term and appears at first contact,
in the legend, never per-row):
- **receipts** — brand/campaign register only. Introduction: "proof
  that it actually happened — the platform keeps the receipts."
- **axioms** — the declared trust anchors (keys, floors, pins).
  Introduction: "everything is proven except your axioms — and your
  axioms are declared."
- **proof-carrying** — whitepaper/architecture register. Work and
  answers ship with checkable evidence; checking is cheap, finding
  is expensive.
- **sound, not complete** — the guarantee, stated in the field's
  words: we never call verified what we didn't check; we willingly
  leave true things unproven.

**INTERNAL-ONLY** (code, specs, ADRs; never on a tenant surface):
seat (surface: the seat's own name, or "decision role") ·
calibration receipt (surface: the sentence — "said 90%, was right
84%") · courier · oversight item · TypedDecisionReceipt · witness
(API name for an evidence object) · conjecture (docs flavor for
UNPROVEN) · resolver/docket (borderline: docket may graduate to
introduced-once if dogfood shows it lands) · raci (surface: the
Ask/Auto dial and the window) · floor (surface: "some things always
Ask — that's a site rule") · ceiling (surface: "Auto must be
earned") · graduation (surface: "a proven track record") · shadow
record (surface: "supervised runs that went well") · calibration
miss (surface: "Auto starts getting things wrong") · demotion
(surface: "take it off Auto" / "switch it back to Ask") · serving
face (surface: "data service").

## The autonomy dial (founder ruling 2026-09-24)

A surface offers exactly TWO autonomy settings: **Ask** and **Auto**.
The safety net is never a third option — the gate class attaches it
to Auto (undoable → runs now with Undo; consequential → runs after a
notice, hold anytime; irreversible → never Auto). Per-instance lines
stay concrete and use "auto-runs" ("auto-runs at 09:00 unless
held" — never "acts 09:00", never "act with a window"). The
promotion prompt is one sentence with the evidence in it ("Approved
5/5, no reversals — switch to Auto?") and both answers go on the
record.

## The tenant vocabulary token

The evidence-noun is tenant-configurable like the accent color
(one-UI-infra): platform default **evidence** (brand register:
receipts); a regulated/nuclear tenant renders **objective evidence /
records** (its auditors' native NQA vocabulary); an agronomy tenant
renders **proof**. Statuses and glyphs never vary — vocabulary skins,
verdicts don't.

## Standard-terminology adoptions (proof theory / verifier framing)

- CONTRADICTED evidence is labeled a **counterexample** everywhere.
- **witness** is the canonical API/spec name for an evidence object.
- **proof-carrying** names the doctrine; **soundness over
  completeness** names the guarantee; "proof" itself is NEVER used
  for empirical verification in technical writing (evidence-checking
  is not deduction; "verified" and "witness" don't overclaim).
- The docket terminator MAY render as ∎ (the end-of-proof tombstone)
  with "that's everything" as its accessible label.

## Enforcement (the can-fail check)

A test walks operator-facing strings (surface copy maps, CLI output,
brief renderer, microcopy/actions files) and fails on any
INTERNAL-ONLY term; introduced-once terms must co-occur with their
introduction at first use or in the legend. Same pattern as the
branding and microcopy guards; the ledger file is the allowlist the
test reads, so adding a term IS the review.
