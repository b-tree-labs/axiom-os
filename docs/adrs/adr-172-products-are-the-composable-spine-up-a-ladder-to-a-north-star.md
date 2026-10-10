# ADR-172: Products are the composable spine; every item is accountable up a ladder to a North Star

**Status:** Accepted (2026-10-06)
**Related:** [ADR-161](adr-161-program-tracking-is-a-composed-clerk.md) (the composed CLERK), [ADR-165](adr-165-program-self-update-and-per-consumer-change-detection.md) (the change log the rollup / accountability reads), [ADR-166](adr-166-program-membership-is-a-principal-not-a-tracker-seat.md) (an owner is a principal), [ADR-167](adr-167-the-program-is-editable-through-its-own-tool.md) (the mutation verbs that edit products), [ADR-171](adr-171-adaptivity-every-external-system-is-an-optional-additive-connector.md) (evidence crosslinks degrade gracefully; new types are data), [ADR-173](adr-173-estimation-is-deterministic-the-model-proposes-a-size-never-a-date.md) (dependency edges are the forecast's gates), `docs/prds/prd-program.md` (R10, R16, R17, R22), `docs/specs/spec-program.md` (§Forward design)

## Context

Program tracking modelled items, lanes, people, and dates. It did not model the
thing a program exists for: its **products**. A program is worthless unless it
produces valuable products — items and lanes are the means, the product is the
end — and without a product as a first-class concept there is no answer to "what
are we actually delivering," nowhere to hang the evidence that a thing shipped, no
way to roll a dozen items into one deliverable, and no apex the whole program
points at.

A program also produces more than software. It produces equipment, models and
datasets, research, communications, sub-programs, and composites of these — so a
product type has to be open, and the type system cannot be code the substrate
owns (prd R10).

The decision has to settle three things before the instrument (ADR-173), the
attachments / deep-links, and the loop ethos are built on it: whether products
are first-class and whether items must be accountable to them; how products
compose; and what sits above the program.

## Decision

**Products are the spine. Every schedule item is accountable to at least one
product — a hard rule, no exception — and products compose infinitely, up a
ladder whose apex is a single North Star.**

1. **A product is a first-class record** (`products[]`): `id`, `name`, `type`,
   `lifecycle`, `owner` (a principal, ADR-166), an optional `target`, the items
   that produce it (via `schedule[].produces`), and `evidence` crosslinks.
   **Every item produces at least one product** — an item that produces nothing
   is a drift finding, not a valid state. (A product with no items is a wish; a
   product past its target with no evidence is a slip. Informational first,
   tightened to refusal per deployment posture.)
2. **Products compose through three edge kinds** — composition (`composed_of`),
   dependency (`depends_on`), and lineage (`derived_from`) — so the product space
   is a directed **graph**, not a tree. Status and evidence roll up the
   composition edges; dependency edges order sequencing (the forecast's gates,
   ADR-173); lineage edges carry provenance. The mind-map view renders the whole
   graph at once.
3. **Seven product-type families seed the taxonomy** — software, physical,
   model/data, research, communication, program, composite — and **the type and
   its lifecycle are deployment data, not code** (R10). A deployment declares the
   families and lifecycles it uses; a new family or lifecycle is configuration;
   the extension names none in code. This is the ADR-171 posture applied to the
   type system.
4. **Evidence is a crosslink, never a copy.** A product points at the system that
   already holds its proof — a release, a model-registry entry, a signed journal
   record, a research-surface page — and crosslink health (R11) watches the link.
   Per ADR-171 the product stands alone: an absent evidence system makes the link
   `unverified`, never breaks the product.
5. **The accountability ladder has an apex: item → product → program → North
   Star.** The North Star is a single visionary statement of the world when the
   program has succeeded — one field (`program.north_star`), surfaced at the top,
   changed rarely. Every item is ultimately accountable to it, and the feedback
   loops (R22) measure distance toward it.

## Options considered

- **Items and lanes only, no product concept.** The status quo; cannot answer
  "what are we delivering," cannot hang evidence or roll up, has no apex.
  Rejected.
- **A product is a label / tag on items.** Cheap, but a tag does not compose,
  does not carry its own lifecycle / owner / target / evidence, and cannot be a
  node in a graph. Rejected.
- **Products as a tree (composition only).** Simpler, but real products depend on
  and derive from one another across the tree; a tree cannot express buy-vs-build
  (a dependency on an off-the-shelf product) or lineage. Rejected in favour of the
  three-edge graph.
- **Make the every-item-produces-a-product rule advisory only.** Rejected as the
  default stance: the rule *is* the spine, so it is hard from day one; the
  informational-then-refusal ramp is a deployment-posture dial, not a reason to
  weaken the model.

## Consequences

- The `axiom.program` schema gains `products[]`, `schedule[].produces`, and
  `program.north_star` additively (the product graph is the schema's next
  revision, `axiom.program/0.2`; a `0.1` file loads unchanged). Id references —
  edges, `produces`, `owner` — are checked on load like every other reference.
- Rollup, the mind-map, the product-detail view, and the accountability drift
  findings are renders / checks over this graph — no new engine (ADR-171's
  anti-monster posture; detailed in the spec's §Forward design and ADR-173).
- A product's owner is a principal (ADR-166), editable through the mutation verbs
  (ADR-167), which gain product nouns; ownership-over-time applies to products as
  to items.
- Type families and lifecycles being data means the loop ethos (R22) is
  expressible without code: a lifecycle simply continues past *delivered* into
  *in use → iterating → adopted*.
- What this commits us to: the product graph becomes the backbone the instrument
  (ADR-173), the attachments / deep-links, and the loop ethos all hang on, so
  changing the edge kinds later is a schema migration — which is why they are
  decided here.
