# Program tracking: the design

*The thinking behind the program tracker. The durable ideas, not the passing details.*

**Status:** Living  •  **Last updated:** 2026-10-06

This document is the readable design narrative for program tracking: what the
tracker is for, the principles it runs on, and the shape it is growing into.
The tracker itself shows the plan; this explains the plan behind the plan. It
is the companion to the formal canon, which lives in the platform as a product
requirements document, a technical specification, and a set of decision
records, linked at the end. Those are the source of truth. This is the "why it
is the way it is."

## The North Star

Everything in the tracker ladders up to one statement of the world when the
program has succeeded. An item is accountable to a product, a product to the
program, and the program to this.

A North Star is a single, timeless, qualitative statement of that success. It
is written in plain language about the people a program serves and what they
will be able to rely on when the work is done. It is deliberately not a metric
and not a date: a metric is a measurement of progress toward the goal, and a
date is a bet about pace, while the North Star is the goal itself. Because it
describes an end state rather than a quarter's push, it does not change when the
plan changes. It is the fixed point the rest of the plan is allowed to move
against.

Its job in the model is to be the apex of accountability. Every other thing the
tracker holds can be traced upward until it reaches the North Star, and anything
that cannot be traced there is, by definition, either not yet connected to the
mission or a sign that the mission statement is incomplete. A program may also
keep a compressed restatement of its North Star, a single sentence that captures
the same end state in a form people can repeat from memory, but that is a
convenience, not a second goal.

The accountability ladder, from the smallest unit of work to the mission, reads:

- North Star: the world when the program has succeeded. Timeless and
  qualitative.
- Objective: a time-boxed goal that advances the North Star.
- Key Result: the measured outcome that says whether the objective was met.
- Product: what the program ships to move a key result.
- Item: a unit of work that builds a product.

Read upward, an item builds a product, a product moves a key result, a key
result measures an objective, and an objective advances the North Star. Read
downward, the North Star sets the objectives in play, the objectives name the
key results that would prove them, the key results pull products into being, and
the products are built out of items. The middle rung of the ladder, the program,
is really this working set: the objectives in play and the key results that
measure them.

## Objectives and key results

The North Star is timeless, so it does not tell anyone what to do this quarter.
Objectives and key results are the measurable layer between it and the work. An
objective is a time-boxed goal that advances the North Star. A key result is the
measured outcome that says whether the objective was met. Products are what the
program ships to move its key results, and every item builds a product, so the
full chain of accountability runs from an item, up through its product, to a key
result, to the objective it serves, to the North Star.

Because key results are measured, they are where progress becomes a number
rather than a feeling, and they sharpen everything downstream. Forecasting
answers "when" against a key result's target rather than against a vague sense of
how far along the work is. A piece of emergent work is judged by whether it moves
a key result, not merely by whether it feels aligned. The question "is this worth
doing" gets a sharper form: "which key result does this move, and by how much."

Objectives and key results are data in the same model as everything else, not a
separate planning system bolted on the side. The program's data holds an
`objectives[]` list and a `key_results[]` list exactly as it holds products and
items, and a key result rolls up from the products and items beneath it. The
practical consequence is that the program's own goals stay as current as the
work that feeds them: nobody maintains a goals document in parallel with the
tracker, because the goals are part of the tracker, and they move when the work
moves.

## The ethos

### Delivery is a beginning, not an end

A product ships minimal and viable, then is used and improved toward adoption.
Delivery starts a loop; it does not close one. The loops nest at feature,
product, and program scale. The working rhythm is to loop hard, ship the
smallest useful thing, learn from how it is used, and let that feedback drive
what comes next. A tracker built on this ethos treats a shipped product as a
live thing with a next step, not as a line that has been crossed off.

### The work you already do is the report

Nobody writes status reports. Merge requests, model registrations, signed
records, and a contributor's own wiki become the record automatically. The
tracker meets people where they already work and ingests what they produce,
rather than asking anyone to enter the same thing twice. A status report is a
second description of work that already described itself; removing it removes a
chore and, with it, the decay that comes when a chore is skipped.

### The system improves itself

The tracker calibrates its estimates and its priorities from what actually
happened. It learns which deliveries got adopted and which estimates held, so it
gets better at forecasting and better at advising, because it measures the result
of its own advice rather than assuming it was right.

### Nothing goes stale

Every piece of content carries when it was last confirmed and by what. Most of it
refreshes itself: anything tied to work that flows through the feeders stays
current without anyone touching it. What cannot refresh on its own is given a
freshness clock, and when a piece ages past its threshold the tracker raises a
short "is this still true?" that lands on the program head's follow-up list
rather than quietly rotting on the page. Staleness is measured and shown at every
scale, from a single line to a whole area, so it can never creep in unseen. The
goal is simple and absolute: no content on the tracker is ever stale. This is the
freshness invariant, and it is the reason the other principles are safe to lean
on, because a record that meets people where they work is only worth trusting if
it cannot silently go out of date.

## The spine: products

A program is worth what it ships, so products are the spine, not a side list.
Every item is accountable to a product, with no exception, which is what keeps
the ladder from having a gap between work and mission.

Products compose without limit, and they relate three ways: composition, where a
product is built out of other products; dependency, where a product needs another
to be in place first; and lineage, where a product descends from or supersedes an
earlier one. Those three relations make the product space a graph, and the
tracker can draw that same graph as a tree, a mind map, or a roadmap depending on
what a reader needs to see.

The kinds of product are configuration, not code. A product might be software, a
document, a piece of installed equipment, a dataset, a model, a published result,
or any other thing a program delivers, and the set of kinds is data a program
edits rather than a fixed list in the tracker. That is how one tracker serves
programs that ship very different things.

Products have a lifecycle of their own: a product is active while it is current,
legacy once a newer product has taken its place but it is still in use, and
superseded once it has been fully replaced. The lifecycle is visible, so a reader
can tell a living product from one that is kept only for continuity.

## The roadmap is an instrument

The default is a clean static roadmap, printed on demand, and that is all most
people need. It is legible on a page, it does not move while someone is reading
it, and it does not ask the reader to operate anything.

Underneath, for those who reach for it, the roadmap can be reshaped, searched,
and modeled against. A what-if is a non-destructive fork of the plan, drawn
beside the real one so the two can be compared without the exploration ever
touching the plan of record. The agent can build such a fork from a
plain-language request and show what it does to the dates. The complexity is
buried until it is wanted, so the first experience stays simple and the power is
there for the reader who needs it. The roadmap is an instrument you can pick up,
not a dashboard you have to learn before you can read the plan.

## When will it be ready

The most common question a program gets deserves a real answer. The date math is
deterministic. It is computed from measured pace, sized work, resourcing, and the
dependency gates, and the language model never invents a date. The model proposes
a size for a piece of work, and occasionally asks one short question when the
size is genuinely unclear; everything else is pulled from the data. The answer is
a range with its assumptions attached, never a false-precise single day. A
deterministic estimate can be audited and improved, where a guessed date can only
be believed or doubted, and keeping the model on sizing rather than on dating is
what keeps the whole estimate honest.

## Shaping by conversation

A program is shaped by a few decisions about what matters most and what can wait.
Those decisions get made in a conversation with the agent. The agent turns a
decision into proposed changes, shows the consequence of each, and enacts it only
once a person approves. Nobody tweaks dials by hand, and nothing is reshaped
silently: the flow is propose, then consequence, then approve, then enact, and
every step is logged, so the record of how the program came to look the way it
does is as durable as the program itself.

## Maximally adaptive, almost nothing required

This is the tracker's defining posture. The program's own data file is the
authoritative source of truth, and every part of the tool works with no external
system present. A code host, a tracker of record, a wiki, any particular chat
tool or coding harness, a directory, a vault: each is an optional connector, used
only where it is configured, available, and genuinely additive. When one is
missing or cannot be reached, the tracker says so plainly and carries on, rather
than breaking. A connector that degrades gracefully is one a program can adopt
without betting its tracker on it, and that is the condition that lets the tracker
run anywhere.

New sources and new kinds of product are data and configuration, not new code, so
the program adapts to whatever a given deployment has and to how each person
already works, without a release in between.

Because the data is structured and owned by the program, it travels out as easily
as it comes in. Any list or view exports to a spreadsheet, the roadmap prints on
demand, and a scoped snapshot hands off to an agent to turn into a slide deck, a
report, or a status note. Export is a projection of the one source, not a second
copy that then has to be kept in step. There is one source of truth and many
views of it, rather than many copies drifting apart.

## A complete experience

Every contributor has a path from where they enter to what they leave with, and
those paths are checked so that none dead-ends. A newcomer lands on one simple
page and finds a first task. Everyone else reaches their outcome through the
surface their role already touches, and nobody has to learn the whole system to
use their part of it.

Access comes in tiers, so a program can be shown without being exposed. A member
sees it in full. A partner sees their own scoped slice and nothing beyond it. A
guest, someone the program wants to keep informed or show the work to, sees a
curated, read-only view and nothing more. Anonymous sees nothing. The same gate
serves all four, so sharing a link is always safe, because the link cannot reveal
more than the recipient's tier allows.

A contributor without tracker access is still tracked, and still reads their own
status, through the node rather than through the tracker. Participation and access
are separate facts: a person can be fully part of a program, credited and
accounted for, without ever holding a tracker seat, and the design treats the
absence of a seat as a routing detail rather than as a reason someone falls out of
the record.

## Emergent work and divergence

In a program where agents and people both do work, work constantly gets done that
was never planned. The tracker catches it rather than letting it vanish. The
feeders surface work that maps to no tracked item, which is emergent work, the
work-level sibling of discovering a contributor who was never added. Each piece is
positioned against the North Star and the key results: the question is whether it
ladders up to a product and the mission, or pulls away from them. It is then
routed to a person, who adopts it, often as a new product, which is captured
serendipity; parks it as a distraction; or defers it. The decision is logged with
its reason, so the choice can be revisited and the pattern of such choices can be
read later.

A divergence view shows discovery against drift over time, so a program can see
whether its unplanned work is mostly fruitful exploration or mostly wandering.
Strictness comes from visibility, not from prohibition. Nothing is forbidden, no
side trail stays invisible, and adoption is one cheap step, so serendipity is
preserved rather than policed. A program that forbids unplanned work loses the
discoveries along with the distractions; a program that makes unplanned work
visible keeps the discoveries and can still name the distractions for what they
are.

## Lifecycle for people and products

People and products both have a lifecycle, and showing it plainly is what keeps a
long-running program legible.

A contributor is active while they are working, paused when they step away for a
time, an alumnus once they have moved on with their contributions credited, and
historical once they belong to the program's past rather than its present. A
product is active while it is current, legacy once something newer carries the
load but it is still in use, and superseded once it has been fully replaced.
Moving through these states is normal, and the record keeps the history rather
than overwriting it, so credit and continuity both survive the change.

Absence is a follow-up, not a judgment. A tracked actor with no current activity
is not scolded and not quietly dropped. The tracker raises a gentle note that
rolls into the program head's follow-up list, the same list the freshness
invariant feeds, so a quiet stretch becomes a question to ask rather than either a
silent gap in the record or an accusation on the page.

## Meetings, and the platform as an advantage

Meetings are program metadata. Where a node has a calendar connector enabled, an
agent pulls the relevant events during its regular scan, the same way it reads
commits and merge requests, and each event attaches to the items it concerns.
Anyone can click through to a meeting from the work it touched, and a newcomer can
ask to be added to one.

But that a meeting simply happened is worth little on its own. The value is in what
the meeting produced: its notes, its contributions, and above all the decisions
and commitments made in it, which become real program updates and can themselves
surface as emergent work. Recording is consent-based, and a meeting with no
captured content stays a calendar entry, not a status. Bare metadata is the floor;
content is the signal.

Where a node runs the data platform, meeting notes, transcripts, and documents
ingest through the medallion, bronze to silver to gold, so the gold layer can
surface the decisions and action items a meeting produced, with provenance, under
the same serving gate that enforces the member, partner, and guest tiers. This
stays optional by design. The data file alone runs everything core; the calendar
and the platform are additive power where they exist, and their absence costs a
program the richer view without costing it the tracker.

## Where this is written down

The authoritative versions of all of this live in the platform as a product
requirements document, a technical specification, and a set of decision records.
They are the source of truth; this document is the readable summary. Detailed
designs and mockups, which change as the work iterates, live in the working design
documents rather than here. This document holds only what is meant to last.

- Product requirements: [`prd-program.md`](../prds/prd-program.md) (requirements
  R1 through R25, read under the adaptivity posture).
- The OKR layer: [`prd-okrs-2026.md`](../prds/prd-okrs-2026.md) (objectives and
  key results as data in the same model).
- Technical specification: [`spec-program.md`](../specs/spec-program.md) (how
  the subsystem is built).
- Decision records:
  [ADR-161](../adrs/adr-161-program-tracking-is-a-composed-clerk.md) (the
  composed clerk),
  [ADR-162](../adrs/adr-162-the-watcher-primitive.md) (the watcher primitive),
  [ADR-163](../adrs/adr-163-steer-and-log-are-two-projections-of-one-journal.md)
  (steer and log as two projections of one journal),
  [ADR-165](../adrs/adr-165-program-self-update-and-per-consumer-change-detection.md)
  (self-update and per-consumer change detection),
  [ADR-166](../adrs/adr-166-program-membership-is-a-principal-not-a-tracker-seat.md)
  (membership is a principal, not a tracker seat),
  [ADR-167](../adrs/adr-167-the-program-is-editable-through-its-own-tool.md)
  (editable through its own tool),
  [ADR-171](../adrs/adr-171-adaptivity-every-external-system-is-an-optional-additive-connector.md)
  (adaptivity: every external system is an optional, additive connector),
  [ADR-172](../adrs/adr-172-products-are-the-composable-spine-up-a-ladder-to-a-north-star.md)
  (products are the composable spine, up a ladder to a North Star),
  [ADR-173](../adrs/adr-173-estimation-is-deterministic-the-model-proposes-a-size-never-a-date.md)
  (estimation is deterministic; the model proposes a size, never a date), and
  [ADR-176](../adrs/adr-176-emergent-work-and-divergence.md) (emergent work and
  divergence).
