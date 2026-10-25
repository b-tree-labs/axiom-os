# ADR-132 — Sensing faults: detect at the edge, carry on the reading, refuse to be ignored

- **Status:** Proposed — 2026-09-24
- **Context owner:** data platform
- **Relates to:** ADR-128 (medallion tier boundaries; silver transforms 2
  *clean* and 9 *validate*), ADR-027 (channel map as the site's declared
  seam), the `d8f.emit/1` emitter contract.

## Context

A scan of every channel on a live install — 220 (site, channel) pairs,
70,110,932 rows — found instrument fault codes being served as
measurements, with a declared unit, in bulk:

| channel | unit | real max | **served max** | at fault code |
|---|---|---|---|---|
| `…GAS:DT01:TempC` | degC | 30.25 | **3276.75** | 16,898 / 78,791 = **21.4%** |
| `…GAS:DT41:TempC` | degC | 28.55 | **3276.75** | 18.0% |
| `…GAS:DT11:TempC` | degC | 33.40 | **3276.75** | 17.5% |
| `…GAS:DT01:Humidity` | %RH | 58.18 | **655.35** | 9.5% |
| `…HEAT:TC-DT2_1` | degC | 70.03 | **65535** | 11 rows |

`65535` is `2**16 - 1`; `655.35` and `3276.75` are the same word under a
fixed-point divisor. A mean of `DT01:TempC` is therefore about 700 degC for
a loop that runs at 25. On a figure, one such sample sets the axis maximum
and the real signal collapses to a flat line along the bottom. **This is
wrong, not noisy.**

### The mechanism to prevent it already exists and is not used

`silver.signals` has carried a `quality` column since it was created. Its
values today:

| quality | rows |
|---|---|
| `good` | 69,781,245 |
| `uncertain` | 329,677 |
| `bad` | **9** |
| `ok` | **1** |

Every one of the 127,377 fault-code rows carries `quality = 'good'`. So do
all 55,027 rows of a mass-flow channel reading negative — 99% of that
channel's history. `bad` has fired nine times in seventy million rows,
which is not a check passing; it is a check nobody wired up.

### Three specific defects behind that

**The vocabulary is split, and the platform's own default violates the
contract it publishes.** The emitter contract validates `quality` against
`("good", "uncertain", "bad")` and refuses anything else. The platform's
DDL declares `quality text NOT NULL DEFAULT 'ok'`, and the conform pass
supplies `"quality": "ok"` when a row carries none. `'ok'` is a value the
partner-facing contract would reject. It appears once in seventy million
rows, which is exactly how long it took for a fourth vocabulary member to
become invisible.

**`uncertain` is being used for something else.** All 329,677 of its rows
are model outputs, where it marks *model* uncertainty. There is a separate
`uncertainty` column for precisely that, carried in the same unit as the
value. So the one quality value in real use is not describing quality.

**Nothing derives quality; conform hard-codes it.** The upsert's defaults
dictionary sets `quality` to a literal. A normalizer may pass one through
from a producer, and none does.

## The taxonomy

Nine distinguishable things go wrong with a reading. Listing them matters
because they are routinely collapsed into "bad data", and they need
different handling:

1. **The device asserts a fault.** An out-of-band code in the value, or a
   status register. The instrument is *telling* us. Highest confidence: it
   is an assertion, not an inference.
2. **The acquisition path failed while the device is fine.** Bus timeout,
   CRC failure, a scan cycle missed, a cached value returned.
3. **The value is outside the declared physical range.** Nothing was
   asserted; we infer.
4. **The value moved faster than the process can.** A thermocouple cannot
   rise 200 degC in 100 ms.
5. **The value is frozen.** Identical across many samples where the process
   varies — a stuck converter or a cache.
6. **The value is saturated.** At the instrument's limit. Technically
   valid, and the true value is unknown *beyond* that limit.
7. **The timestamp is untrustworthy.** Unsynchronised clock, a stamp in the
   future, or a resolution coarser than the sample rate.
8. **The channel is not what it claims.** The map says degC and the device
   is sending raw counts.
9. **There is no reading at all.** Absent, which is not the same as faulted.

### The two axes that matter

These differ along two independent axes, and conflating them is why one
`quality` column has never been usable:

- **Who knows** — the device asserted it (1), the acquisition layer knows
  (2, 7), it is inferred from a site's declaration (3, 6), inferred from
  history (4, 5), or inferred from a contract (8).
- **What a consumer may do with the value** — it is meaningless (1, 2), it
  is suspect but may be real (3, 4), it is a *bound* rather than a value
  (6), or it is fine and its *time* is wrong (7).

The second axis is the only one a consumer needs. The first is what a
producer needs in order to fix it. **They must not share a field.**

## Decision

The decisions below are ordered as they were reached: D1–D7 define the
mechanism, D8–D12 answer the questions the first draft left open.

### D1 — `quality` states what a consumer may do. It is closed and small.

| value | meaning | value usable? |
|---|---|---|
| `good` | nothing failed a check | yes |
| `suspect` | may be real; failed an inferred check | excluded from aggregates by default, shown with its reason |
| `bad` | meaningless; the device or the path said so | no |
| `saturated` | the true value is at or beyond this bound | as a bound, never as a point |
| `stale` | repeated, not freshly acquired | as a held value, never as a new observation |

`uncertain` is retired: model uncertainty belongs in the `uncertainty`
column, which already exists and is already in the same unit as the value.

### D2 — the diagnosis goes in `quality_reason`, which is open and grows.

`device.fault_code`, `acq.timeout`, `range.declared`, `rate.declared`,
`frozen.declared`, `clock.unsynced`, `saturation.declared`. One is a
contract with consumers and must never grow; the other is a message to the
producer and must be free to.

### D3 — a `bad` reading has `value = NULL`, with the original kept.

This is the load-bearing decision, and it is the reason to prefer NULL over
a flag every consumer has to remember:

- **SQL already does the right thing with NULL.** `avg`, `sum`, `min` and
  `max` skip it. Every aggregate in the system becomes correct the moment
  the data is, with no query changed anywhere.
- **The exclusion count is free.** `count(*) - count(value)` is how many
  readings were withheld, per group, without anybody writing it.
- **The renderers are already built for it.** The figure's stated principle
  is *gaps are gaps* — a missing point breaks the line rather than being
  interpolated across — and a table already prints an absent value as an em
  dash. A NULL is drawn correctly today.

The raw word is preserved in `raw_value`, because the diagnosis needs it
and because destroying what the instrument sent is not ours to do.

**Quarantine the value; keep the event.** A faulted reading is not deleted.
The row remains, at its timestamp, saying the sensor was faulted then. A
deleted row leaves a gap, and a gap cannot be told from "the loop was off"
— which is a different fact about the world.

### D4 — detect where the fault is known, not where it must be guessed.

| kind | detected by | basis |
|---|---|---|
| device fault code | **the emitter, at the edge** | the channel map's declared fault values |
| acquisition failure | **the emitter** | the driver's own error |
| out of range | conform | declared range |
| rate violation | conform | declared rate limit |
| frozen | conform | declared hold tolerance |
| saturated | conform | declared instrument limits |
| clock | ingest face | arrival vs stamp |

A range check in conform can only *infer* that 3276.75 is a fault, and will
miss any fault code that happens to fall inside a plausible range. The
driver *knows* the module's fault register. Encode it where it is known.

### D5 — a site declares its faults where it already declares its units.

The channel map gains, beside `unit` and `role`:

```toml
[channels."NCDT1:GAS:DT01:TempC"]
unit         = "degC"
fault_values = [3276.75, 65535]   # what this hardware emits when it cannot read
range        = [-20.0, 150.0]     # outside this is suspect, not fatal
saturates_at = [-20.0, 150.0]     # at the limit the true value is beyond it
rate_limit   = 50.0               # degC per second the process can actually move
frozen_after = 300                # identical samples for this many seconds is a hold
```

One change per site, not per channel of code. The map is already the
declared seam for facts only the site knows, and a unit is exactly as
site-specific as a fault code.

### D6 — a consumer may not ignore quality, and the default is safe.

- `gold.signals` carries `quality` and `quality_reason`.
- Aggregates read a projection restricted to usable readings, and report
  what they excluded. "Mean 24.8 degC over 61,893 of 78,791 readings;
  16,898 withheld (device.fault_code)" is an answer. "Mean 703.2 degC" is
  not.
- A figure draws faulted intervals distinctly rather than omitting them, so
  a reader can tell a broken sensor from an idle loop.
- An export carries both columns. A downstream tool that ignores them is
  outside our reach, but it cannot say we did not send them.

### D7 — a fault that persists is an event, not only a column.

Marking rows tells whoever looks. A channel faulted for hours should reach
somebody who was not looking, on the path `gold.ingest_stale` already uses
for a stream that stopped advancing. A sensor that fails silently for four
months is the same failure as a stream that goes quiet for four months, and
we already decided the second one deserves a notification.

## Consequences

- **Backward compatible for producers.** A frame carrying no `quality`
  means `good`, which is today's behaviour. Nothing a partner has already
  shipped breaks, and the conform-side checks catch what a producer has not
  yet declared.
- **`value` becomes nullable.** Today there are zero NULL values stored, so
  nothing in the current data changes shape; what changes is that consumers
  must tolerate NULL, and the renderers already do.
- **The existing `uncertain` rows need a decision.** 329,677 of them mean
  model uncertainty. Mapping them to `good` with the number moved into the
  `uncertainty` column is the faithful reading, but it is a migration over
  somebody's analysis and belongs to whoever relies on it.
- **The emitter contract gains two optional fields**, so the vendored SDK
  on a partner's lab PC changes. That is a version bump on a contract
  partners implement against, and it is the reason this is an ADR rather
  than a patch.
- **`'ok'` must go.** It is in a DDL default, in the conform pass, and in a
  normalizer fallback, and it is not a value the published contract admits.

## D8 — `suspect` is excluded from aggregates by default and always counted

Settled. A reading outside a declared range may be a genuine excursion, and
silently dropping real excursions is its own falsification — so the count
is never optional. An aggregate answers "mean 24.8 degC over 61,893 of
78,791 readings; 16,898 withheld (device.fault_code), 412 suspect
(range.declared)". Including the suspect ones is a deliberate, explicit
request, never the default.

## D9 — a range is not declared-or-not; it has a provenance, and they rank

"Undeclared" was the wrong frame. A plausible range can be arrived at
several ways, they differ in authority, and the strongest available one
wins. Ordered:

| # | provenance | example | authority |
|---|---|---|---|
| 1 | **the device, per reading** | EPICS `.SEVR` / `.STAT` — the record itself says INVALID, HIHI, READ, COMM | an assertion, not a range at all: a verdict |
| 2 | **the device, per channel** | EPICS `HOPR`/`LOPR`, `DRVH`/`DRVL`, `HIHI`/`LOLO`; a datasheet span; a calibration certificate | declared by whoever built it |
| 3 | **the site** | the channel map, written by the operator | declared by whoever runs it |
| 4 | **the encoding** | a 16-bit unsigned word is `[0, 65535]`, and its top is the conventional fault value | arithmetic |
| 5 | **the unit** | `%RH` is `[0, 100]`; absolute temperature is `>= 0`; a fraction is `[0, 1]`; a count is a non-negative integer | dimensional, and never wrong |
| 6 | **the role** | ADR-027 says what a channel MEANS; peers at other sites bound it | cross-site |
| 7 | **an authored limit** | a licensed maximum, a design pressure — ADR-128 E1 reference data | authored |
| 8 | **observed history** | a robust envelope, with confidence growing as coverage grows | inferred |
| 9 | **sibling channels** | three nominally identical sensors, one disagreeing | inferred |
| 10 | **unknown** | and it says so | none |

**The structural point is that these are two different kinds of bound, and
they produce different verdicts:**

- **Impossible** — 4, 5, and the hard parts of 2 and 7. Violating one means
  the value is definitely not a measurement. → `bad`.
- **Implausible** — 3, 6, 8, 9, and the operating parts of 2. Violating one
  means the value may still be a genuine excursion. → `suspect`.

Rows 4 and 5 are free, universal, and need no declaration from anyone. They
alone would have caught `655.35 %RH`, every negative absolute reading, and
every 16-bit fault word. **Shipping those two first buys most of the
protection with no site obligation at all**, and makes rows 1–3 an
improvement rather than a prerequisite.

## D10 — for the site where this was found, the device is already telling us

The SENNA channels are EPICS. EPICS carries a per-sample quality model
natively: `.SEVR` is one of `NO_ALARM`, `MINOR`, `MAJOR`, `INVALID`, and
`.STAT` says why — `HIHI`, `LOLO`, `READ`, `COMM`, `CALC`, `UDF`. It
carries per-channel range natively too: `.EGU` for the unit, `HOPR`/`LOPR`
for display range, `DRVH`/`DRVL` for drive limits, `HIHI`/`LOLO` for alarm
thresholds.

**Nothing in the acquisition path reads any of it.** The EPICS engine-config
reader already notes that `.EGU` and `.DESC` "are one Channel Access sweep
away, which is the ask that actually unblocks charts". That same sweep
returns the range fields, and the archive stores status and severity beside
every sample.

So for the site where fault codes were found being served as measurements:
**the instruments were labelling those readings INVALID all along, and we
discarded the label and then inferred it badly downstream.** The taxonomy
in D1 is therefore not an invention to be adopted by producers — it is
mostly a mapping onto one instruments already speak:

**The mapping keys on `.STAT`, not on `.SEVR`, and getting that backwards
is a real trap.** An earlier draft of this table said `MAJOR`/`MINOR` with
`HIHI`/`LOW` becomes `suspect`. That is wrong, and combined with D8 it is
wrong in the most damaging possible direction: it would exclude **every
genuine process excursion** from every aggregate, biasing every statistic
toward the normal operating range. A `HIHI` alarm is the instrument
correctly reporting that the process went high. That is not a doubt about
the reading; it is the most interesting reading in the series.

The two EPICS fields answer different questions. `.SEVR` says how *urgent*
an alarm is. `.STAT` says *why* — and the reasons fall into two groups that
have nothing to do with each other:

- **Process alarms** — `HIHI`, `HIGH`, `LOW`, `LOLO`, `STATE`, `COS`. The
  instrument is fine and the *process* is out of bounds. The reading is
  good.
- **Acquisition alarms** — `READ`, `WRITE`, `COMM`, `TIMEOUT`, `CALC`,
  `SCAN`, `LINK`, `UDF`, `SIMM`. The reading did not survive being taken.

Severity alone cannot separate them: a `MAJOR` may be `HIHI`, which is
perfect data, or `COMM`, which is a dead link.

| EPICS | `quality` | reason |
|---|---|---|
| `.SEVR = INVALID` (any status) | `bad` | `device.invalid` |
| `.STAT` in the acquisition group, any severity | `bad` | `acq.device` |
| `.STAT` in the process group, any severity | **`good`** | — |
| `.SEVR = NO_ALARM` | `good` | — |

A process alarm is still worth carrying — an operator wants to know the
process was in alarm — but it is a statement about the world rather than
about the reading, so it does not belong in `quality` and is deliberately
left to a separate concern.

**Today only `.SEVR` is captured.** `CaUpdate` has a single `severity`
field whose own comment calls it "severity/status", as though they were one
thing. The field that would disambiguate a hot process from a dead link is
not being read, which is why severity had to be used as a proxy for quality
and why `MINOR` and `MAJOR` both had to land on one label.

Adopt the vocabulary the instruments already speak and map it, rather than
asking every producer to learn ours — but map from the field that carries
the meaning.

## D11 — the vocabulary must earn its keep, and two members now do

`stale` and `saturated` were listed in D1 without consumers, which is a
reason to doubt them. Two searches settled one and left the other honest:

- **`stale` has a consumer, under another name.** The EPICS engine config
  already computes `step_held` per channel — monitored channels are saved
  on change, so a gap means unchanged rather than missing — and the chart
  layer already honours it: both `chart_choice` and `chart_svg` cite "the
  objection this codebase already makes to interpolating a step-held
  channel". What is missing is that `step_held` is a property of the
  *channel* while `stale` is a property of a *reading*. A held value and a
  fresh one are different observations, and today they are indistinguishable
  once written. The consumer exists; the per-reading signal does not.
- **`saturated` has no consumer today**, and one live instance: a channel
  whose maximum is exactly 10000, which is a clamp rather than a reading.
  It stays in the vocabulary only if a consumer is named with it. The
  candidates are an aggregate (a mean over clamped values understates), a
  figure (a bound is drawn differently from a point), and any comparison of
  a model against a measurement that turns out to be a bound.

## Open — still undecided

1. **Should conform refuse a channel with no declared range at all**, or
   admit it as ungraded? D9 changes this question: with the dimensional and
   encoding bounds always available, "no range" is rarer than it looked.
2. **Who declares when the site does not know its own module's fault word?**
   A platform library of common instrument fault codes helps, and is also a
   place to be wrong on somebody else's behalf.
3. **Uncertainty, comprehensively.** See D12.

## D12 — uncertainty: absent must not read as zero, and must not block

Uncertainty and quality are different and both are needed. Quality is a
categorical statement about whether a value may be used. Uncertainty is a
magnitude, in the same unit as the value, saying how well it is known.
Neither substitutes for the other, and the existing `quality = 'uncertain'`
is the two being conflated.

The conform pass already states the principle that matters, in a comment
next to its own default: *"Absent, not zero. Zero uncertainty is a claim of
perfect precision; a source that said nothing made no claim."* That is
exactly the units rule in another coat — **an absent uncertainty must never
render as a precise value**, just as an absent unit must never render as a
dimensionless one.

That gives a way forward that does not block on solving uncertainty
comprehensively:

- **Never require it.** A producer that says nothing has made no claim, and
  that is a legitimate state.
- **Never let its absence read as precision.** A value shown with every
  digit it happens to carry claims a precision nobody asserted.
- **Consume it where it already pays.** `table_spec.Column.uncertainty_from`
  exists and governs significant digits, on the stated grounds that "an
  uncertainty is the only thing that can say a digit is not meaningful". It
  is the consumer, and it is unwired.
- **There is an immediate source.** A model channel and its sigma arrive as
  two separate channels today, and the `uncertainty` column beside the value
  is null. Composing them is ADR-128 silver transform 8, and it turns the
  display-precision consumer on for real data without anyone declaring
  anything new.

Comprehensive uncertainty — propagation through derived quantities,
distinguishing aleatory from epistemic, uncertainty on a model's own
prediction — is a larger programme and is not settled here. What is settled
is that it is not a prerequisite: carry it when it is known, never fake it
when it is not, and never let silence look like precision.
